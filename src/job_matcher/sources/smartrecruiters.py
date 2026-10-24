"""SmartRecruiters lists postings without descriptions; each needs one detail request."""

from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.http import SourceError
from job_matcher.sources.text import html_to_text, infer_remote, iso, join_text, money

NAME = "smartrecruiters"
API = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
LOW_TRUST = True
PAGE, MAX_PAGES = 100, 10
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def careers_url(board):
    return f"https://careers.smartrecruiters.com/{board.token}"


def _location(item):
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    full = text(loc.get("fullLocation")) or ", ".join(
        text(loc.get(k)) for k in ("city", "region", "country") if text(loc.get(k))
    )
    workplace = "hybrid" if loc.get("hybrid") else None
    return full, infer_remote(workplace, loc.get("remote"), full)


def posting(board, item):
    location, remote = _location(item)
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    identifier = text(company.get("identifier")) or board.token
    return Posting(
        title=text(item.get("name")),
        company=board.company or text(company.get("name")) or board.token,
        url=f"https://jobs.smartrecruiters.com/{identifier}/{item.get('id')}",
        location=location or None,
        remote=remote,
        posted_at=iso(item.get("releasedDate")),
        source=NAME,
        source_id=text(item.get("id")),
        detail_ref=text(item.get("id")),
    )


def _page(http, board, offset, limit=None):
    url = f"{API.format(token=board.token)}?limit={limit or PAGE}&offset={offset}"
    payload = require(http.json(url, missing_ok=True), dict)
    if len(payload.get("content") or []) > (limit or PAGE):
        raise SourceError("SmartRecruiters response exceeds the requested page size")
    return payload


def fetch(http, board, horizon_days=None):
    items, total, offset = [], 0, 0
    for _ in range(MAX_PAGES):
        payload = _page(http, board, offset)
        page = [i for i in payload.get("content") or [] if isinstance(i, dict) and i.get("id")]
        total = int(payload.get("totalFound") or 0)
        items += page
        offset += len(page)
        if not page or offset >= total:
            break
    name = next(
        (text((i.get("company") or {}).get("name")) for i in items if i.get("company")), None
    )
    return Listing([posting(board, i) for i in items], name, total, len(items) >= total)


def probe(http, board):
    payload = _page(http, board, 0, limit=10)
    items = [i for i in payload.get("content") or [] if isinstance(i, dict) and i.get("id")]
    name = next(
        (text((i.get("company") or {}).get("name")) for i in items if i.get("company")), None
    )
    # An unknown company returns 200 with no postings, so emptiness means "not found".
    total = int(payload.get("totalFound") or 0)
    return Listing([posting(board, i) for i in items], name, total)


def detail(http, board, stub):
    item = require(
        http.json(f"{API.format(token=board.token)}/{stub.detail_ref}", missing_ok=True),
        dict,
        "Posting no longer listed",
    )
    sections = (item.get("jobAd") or {}).get("sections") or {}
    description = join_text(
        *(
            html_to_text((sections.get(key) or {}).get("text"))
            for key in SECTIONS
            if isinstance(sections.get(key), dict)
        )
    )
    comp = item.get("compensation") if isinstance(item.get("compensation"), dict) else {}
    location, remote = _location(item)
    return stub.model_copy(
        update={
            "description": description,
            "url": text(item.get("postingUrl")) or stub.url,
            "apply_url": text(item.get("applyUrl")) or None,
            "location": location or stub.location,
            "remote": remote if remote is not None else stub.remote,
            "compensation": money(
                comp.get("min"), comp.get("max"), comp.get("currency"), comp.get("period")
            )
            or None,
            "posted_at": iso(item.get("releasedDate")) or stub.posted_at,
        }
    )
