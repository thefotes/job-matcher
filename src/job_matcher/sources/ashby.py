from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.text import html_to_text, infer_remote, iso

NAME = "ashby"
API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def careers_url(board):
    return f"https://jobs.ashbyhq.com/{board.token}"


def _location(job):
    names = [text(job.get("location"))]
    for extra in job.get("secondaryLocations") or []:
        if isinstance(extra, dict):
            names.append(text(extra.get("location") or extra.get("locationName")))
    return ", ".join(dict.fromkeys(n for n in names if n))


def _compensation(job):
    comp = job.get("compensation") or {}
    if not isinstance(comp, dict):
        return None
    return (
        text(comp.get("compensationTierSummary"))
        or text(comp.get("scrapeableCompensationSalarySummary"))
        or None
    )


def posting(board, job):
    location = _location(job)
    return Posting(
        title=text(job.get("title")),
        company=board.company or board.token,
        description=html_to_text(job.get("descriptionHtml")) or text(job.get("descriptionPlain")),
        url=text(job.get("jobUrl")) or f"{careers_url(board)}/{job.get('id')}",
        apply_url=text(job.get("applyUrl")) or None,
        location=location or None,
        remote=infer_remote(job.get("workplaceType"), job.get("isRemote"), location),
        compensation=_compensation(job),
        posted_at=iso(job.get("publishedAt")),
        updated_at=iso(job.get("updatedAt")),
        source=NAME,
        source_id=text(job.get("id")),
    )


def fetch(http, board, horizon_days=None):
    payload = require(http.json(API.format(token=board.token), missing_ok=True), dict)
    jobs = [
        j
        for j in payload.get("jobs") or []
        if isinstance(j, dict) and j.get("id") and j.get("isListed", True)
    ]
    return Listing([posting(board, j) for j in jobs], None, len(jobs))


probe = fetch
