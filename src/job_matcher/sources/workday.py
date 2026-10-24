"""Workday career sites: ``{tenant}.{shard}.myworkdayjobs.com/{site}``.

The list endpoint pages 20 postings at a time, newest first, with relative dates
("Posted 3 Days Ago") and no description; each description is a separate request.
Listing therefore stops once a whole page is older than the requested horizon.
"""

from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.http import SourceError
from job_matcher.sources.text import days_ago, html_to_text, infer_remote, relative_days

NAME = "workday"
LOW_TRUST = True
PAGE, MAX_PAGES = 20, 25


def host(board):
    return f"https://{board.token}.{board.shard}.myworkdayjobs.com"


def careers_url(board):
    return f"{host(board)}/{board.site}"


def _api(board):
    return f"{host(board)}/wday/cxs/{board.token}/{board.site}"


def _source_id(board, path):
    # Requisition numbers repeat across tenants, so the identity carries the tenant.
    return f"{board.token}:{path.rstrip('/').rsplit('/', 1)[-1]}"


def posting(board, item):
    path = text(item.get("externalPath"))
    age = relative_days(item.get("postedOn"))
    location = text(item.get("locationsText"))
    return Posting(
        title=text(item.get("title")),
        company=board.company or board.token,
        url=f"{careers_url(board)}{path}",
        # "3 Locations" is a count, not a place; the detail request names them.
        location=None if location.lower().endswith("locations") else location or None,
        remote=infer_remote(location=location),
        posted_at=days_ago(age).isoformat() if age is not None else None,
        source=NAME,
        source_id=_source_id(board, path),
        detail_ref=path,
    )


def _page(http, board, offset):
    body = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}
    payload = require(http.json(f"{_api(board)}/jobs", body, missing_ok=True), dict)
    if len(payload.get("jobPostings") or []) > PAGE:
        raise SourceError("Workday response exceeds the requested page size")
    return payload


def fetch(http, board, horizon_days=None):
    items, total, complete, capped = [], 0, False, True
    for number in range(MAX_PAGES):
        payload = _page(http, board, number * PAGE)
        page = [i for i in payload.get("jobPostings") or [] if i.get("externalPath")]
        total = int(payload.get("total") or total or 0)
        items += page
        ages = [relative_days(i.get("postedOn")) for i in page]
        if not page or (number + 1) * PAGE >= total:
            complete, capped = True, False
            break
        if horizon_days is not None and all(a is not None and a > horizon_days for a in ages):
            capped = False
            break
    unique = {i["externalPath"]: i for i in items}
    return Listing([posting(board, i) for i in unique.values()], None, total, complete, capped)


def probe(http, board):
    payload = _page(http, board, 0)
    page = [i for i in payload.get("jobPostings") or [] if i.get("externalPath")]
    return Listing([posting(board, i) for i in page], None, int(payload.get("total") or 0))


def detail(http, board, stub):
    payload = require(
        http.json(f"{_api(board)}{stub.detail_ref}", missing_ok=True),
        dict,
        "Posting no longer listed",
    )
    info = payload.get("jobPostingInfo") or {}
    locations = [text(info.get("location"))] + [
        text(x) for x in info.get("additionalLocations") or []
    ]
    location = "; ".join(dict.fromkeys(x for x in locations if x)) or stub.location
    return stub.model_copy(
        update={
            "title": text(info.get("title")) or stub.title,
            "description": html_to_text(info.get("jobDescription")),
            "url": text(info.get("externalUrl")) or stub.url,
            "location": location,
            "remote": infer_remote(info.get("remoteType"), None, location or ""),
            "posted_at": stub.posted_at
            if not info.get("startDate")
            else f"{info['startDate']}T00:00:00+00:00",
        }
    )
