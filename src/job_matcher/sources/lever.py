from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.text import html_to_text, infer_remote, iso, join_text, money

NAME = "lever"
API = "https://api.lever.co/v0/postings/{token}?mode=json"


def careers_url(board):
    return f"https://jobs.lever.co/{board.token}"


def _location(categories):
    names = [text(categories.get("location"))]
    names += [text(n) for n in categories.get("allLocations") or []]
    return ", ".join(dict.fromkeys(n for n in names if n))


def _description(job):
    # descriptionPlain is only the opening; the body continues in lists and additional.
    sections = [html_to_text(job.get("description")) or text(job.get("descriptionPlain"))]
    for item in job.get("lists") or []:
        if isinstance(item, dict):
            sections.append(join_text(text(item.get("text")), html_to_text(item.get("content"))))
    sections.append(html_to_text(job.get("additional")) or text(job.get("additionalPlain")))
    return join_text(*sections)


def posting(board, job):
    categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
    location = _location(categories)
    salary = job.get("salaryRange") if isinstance(job.get("salaryRange"), dict) else {}
    compensation = money(
        salary.get("min"), salary.get("max"), salary.get("currency"), salary.get("interval")
    ) or text(job.get("salaryDescriptionPlain"))
    return Posting(
        title=text(job.get("text")),
        company=board.company or board.token,
        description=_description(job),
        url=text(job.get("hostedUrl")) or f"{careers_url(board)}/{job.get('id')}",
        apply_url=text(job.get("applyUrl")) or None,
        location=location or None,
        remote=infer_remote(job.get("workplaceType"), None, location),
        compensation=compensation or None,
        posted_at=iso(job.get("createdAt")),
        updated_at=iso(job.get("updatedAt")),
        source=NAME,
        source_id=text(job.get("id")),
    )


def fetch(http, board, horizon_days=None):
    payload = require(http.json(API.format(token=board.token), missing_ok=True), list)
    jobs = [j for j in payload if isinstance(j, dict) and j.get("id")]
    return Listing([posting(board, j) for j in jobs], None, len(jobs))


probe = fetch
