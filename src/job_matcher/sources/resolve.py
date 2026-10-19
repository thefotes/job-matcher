"""Resolve a company name or careers URL to a verified public ATS board.

An HTTP 200 for a guessed token is not proof the board belongs to the company: "Capital
One" guessed as Lever token ``capital`` reaches a different firm. Verification is therefore
deterministic and conservative. A guess is accepted only when the token is the exact
normalized company name on a trusted provider, or the board names itself as the company.
Everything else comes back as a candidate for the user to confirm by its careers URL.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from job_matcher.sources import adapter, low_trust
from job_matcher.sources.base import Board
from job_matcher.sources.http import SourceError

CORE = ("greenhouse", "ashby", "lever")
OPTIONAL = ("smartrecruiters", "recruitee")
CORE_TOKENS, OPTIONAL_TOKENS = 3, 2

_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|ltd|limited|the|company|"
    r"technologies|technology|group|holdings|gmbh|s\.a|plc|lp|llp)\b",
    re.I,
)
_SEGMENT = r"([A-Za-z0-9][A-Za-z0-9_.-]*)"


def normalize_company(name):
    value = re.sub(r"\(.*?\)", " ", (name or "").lower()).replace("&", " and ")
    value = _SUFFIX.sub(" ", re.sub(r"[^a-z0-9 ]+", " ", value))
    return " ".join(value.split())


def token_candidates(name):
    """Likely board tokens, most likely first: compact, hyphenated, first word(s)."""
    words = normalize_company(name).split()
    if not words:
        return []
    candidates = ["".join(words), "-".join(words), words[0]]
    if len(words) > 2:
        candidates += ["".join(words[:2]), "-".join(words[:2])]
    return list(dict.fromkeys(c for c in candidates if len(c) >= 2))


def same_company(a, b):
    return bool(a and b) and normalize_company(a).replace(" ", "") == normalize_company(b).replace(
        " ", ""
    )


def infer_board(url):
    """Board from a careers/job/API URL of a supported ATS, or None if unrecognized."""
    raw = (url or "").strip()
    if not re.match(r"https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    hostname = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]

    def first(*skip):
        segments = [p for p in path if p.lower() not in skip]
        return segments[0] if segments and re.fullmatch(_SEGMENT, segments[0]) else None

    token = None
    if hostname.endswith("myworkdayjobs.com"):
        match = re.fullmatch(r"([a-z0-9-]+)\.(wd\d{1,3})\.myworkdayjobs\.com", hostname)
        if not match:
            return None
        if path[:2] == ["wday", "cxs"] and len(path) >= 4:
            site = path[3]
        else:
            site = next((p for p in path if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)), None)
        if not site or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
            return None
        return Board(provider="workday", token=match[1], shard=match[2], site=site)
    if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
        query = parse_qs(parts.query)
        if "for" in query:
            token = query["for"][0]
        elif hostname.startswith("boards-api."):
            token = path[2] if path[:2] == ["v1", "boards"] and len(path) > 2 else None
        else:
            token = first("embed", "job_board")
        provider = "greenhouse"
    elif hostname in {"jobs.lever.co", "api.lever.co"}:
        token = path[2] if path[:2] == ["v0", "postings"] and len(path) > 2 else first()
        provider = "lever"
    elif hostname == "jobs.ashbyhq.com":
        token, provider = first(), "ashby"
    elif hostname == "api.ashbyhq.com":
        token = path[2] if path[:2] == ["posting-api", "job-board"] and len(path) > 2 else None
        provider = "ashby"
    elif hostname in {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"}:
        token, provider = first(), "smartrecruiters"
    elif hostname == "api.smartrecruiters.com":
        token = path[2] if path[:2] == ["v1", "companies"] and len(path) > 2 else None
        provider = "smartrecruiters"
    elif hostname.endswith(".recruitee.com") and hostname.count(".") == 2:
        token, provider = hostname.split(".")[0], "recruitee"
    else:
        return None
    if not token or not re.fullmatch(_SEGMENT, token):
        return None
    return Board(provider=provider, token=token)


def verification(company, board, board_name):
    """(verified, method) for a guessed board. Ported from connections-finder stage 2."""
    compact = normalize_company(company).replace(" ", "")
    hyphen = "-".join(normalize_company(company).split())
    if same_company(board_name, company):
        return True, "board-name"
    if low_trust(board.provider):
        return False, "low-trust-provider"
    if board.token.lower() in {compact, hyphen}:
        return True, "exact-token"
    words = normalize_company(company).split()
    if len(words) > 1 and board.token.lower() == words[0]:
        return False, "first-word-only"
    if compact.startswith(board.token.lower()):
        return False, "truncated-token"
    return False, "ambiguous-token"


def summarize(board, listing):
    module = adapter(board.provider)
    return {
        "provider": board.provider,
        "token": board.token,
        "shard": board.shard,
        "site": board.site,
        "careers_url": module.careers_url(board),
        "board_name": listing.board_name,
        "job_count": listing.total if listing.total is not None else len(listing.postings),
        "sample_titles": [p.title for p in listing.postings[:5] if p.title],
    }


def probe(http, board):
    """Listing for a live board with postings, None for a missing or empty board."""
    try:
        listing = adapter(board.provider).probe(http, board)
    except SourceError as exc:
        if exc.status in {404, 410}:
            return None
        raise
    return listing if listing.postings else None


def search(http, company, workers=8):
    """Probe likely tokens across providers; return hits with their verification."""
    tokens = token_candidates(company)
    if not tokens:
        raise ValueError("Company name has no usable characters")
    boards = [Board(provider=p, token=t) for t in tokens[:CORE_TOKENS] for p in CORE if _ok(t)] + [
        Board(provider=p, token=t) for t in tokens[:OPTIONAL_TOKENS] for p in OPTIONAL if _ok(t)
    ]

    def attempt(board):
        try:
            return board, probe(http, board), None
        except SourceError as exc:
            return board, None, str(exc)

    with ThreadPoolExecutor(workers) as pool:
        outcomes = list(pool.map(attempt, boards))
    hits, errors = [], []
    for board, listing, error in outcomes:
        if error:
            errors.append({"provider": board.provider, "token": board.token, "error": error})
        elif listing:
            verified, method = verification(company, board, listing.board_name)
            hits.append({**summarize(board, listing), "verified": verified, "method": method})
    return hits, errors


def _ok(token):
    try:
        Board(provider="greenhouse", token=token)
        return True
    except ValueError:
        return False
