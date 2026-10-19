from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.text import html_to_text, infer_remote, iso, money

NAME = "greenhouse"
API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def careers_url(board):
    return f"https://job-boards.greenhouse.io/{board.token}"


def _compensation(metadata):
    for item in metadata or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if not any(word in name for word in ("salary", "compensation", "pay")):
            continue
        value = item.get("value")
        if isinstance(value, dict):
            found = money(
                value.get("min_value"), value.get("max_value"), value.get("unit"), ""
            ) or text(value.get("amount"))
            if found:
                return found
        elif text(value):
            return text(value)
    return None


def posting(board, job):
    location = (
        (job.get("location") or {}).get("name") if isinstance(job.get("location"), dict) else ""
    )
    if not location:
        location = ", ".join(
            text(o.get("name")) for o in job.get("offices") or [] if isinstance(o, dict)
        )
    return Posting(
        title=text(job.get("title")),
        company=board.company or text(job.get("company_name")) or board.token,
        description=html_to_text(job.get("content")),
        url=text(job.get("absolute_url")) or f"{careers_url(board)}/jobs/{job.get('id')}",
        location=location or None,
        remote=infer_remote(location=location),
        compensation=_compensation(job.get("metadata")),
        posted_at=iso(job.get("first_published")),
        updated_at=iso(job.get("updated_at")),
        source=NAME,
        source_id=text(job.get("id")),
    )


def fetch(http, board, horizon_days=None):
    payload = require(
        http.json(API.format(token=board.token) + "?content=true", missing_ok=True), dict
    )
    jobs = [j for j in payload.get("jobs") or [] if isinstance(j, dict) and j.get("id")]
    name = next((text(j.get("company_name")) for j in jobs if j.get("company_name")), None)
    return Listing([posting(board, j) for j in jobs], name, len(jobs))


def probe(http, board):
    payload = require(http.json(API.format(token=board.token), missing_ok=True), dict)
    jobs = [j for j in payload.get("jobs") or [] if isinstance(j, dict)]
    name = next((text(j.get("company_name")) for j in jobs if j.get("company_name")), None)
    if not name:
        board_info = http.json(API.format(token=board.token)[: -len("/jobs")], missing_ok=True)
        name = text((board_info or {}).get("name")) or None
    postings = [
        Posting(title=text(j.get("title")), company="", source=NAME, source_id=text(j.get("id")))
        for j in jobs
    ]
    return Listing(postings, name, len(jobs))
