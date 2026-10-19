from job_matcher.sources.base import Listing, Posting, require, text
from job_matcher.sources.text import html_to_text, infer_remote, iso, join_text, money

NAME = "recruitee"
API = "https://{token}.recruitee.com/api/offers/"
LOW_TRUST = True


def careers_url(board):
    return f"https://{board.token}.recruitee.com/"


def _location(offer):
    if text(offer.get("location")):
        return text(offer["location"])
    names = [text(loc.get("name")) for loc in offer.get("locations") or [] if isinstance(loc, dict)]
    return ", ".join(n for n in names if n)


def posting(board, offer):
    location = _location(offer)
    workplace = (
        "remote"
        if offer.get("remote")
        else "hybrid"
        if offer.get("hybrid")
        else "onsite"
        if offer.get("on_site")
        else None
    )
    salary = offer.get("salary") if isinstance(offer.get("salary"), dict) else {}
    return Posting(
        title=text(offer.get("title")),
        company=board.company or text(offer.get("company_name")) or board.token,
        description=join_text(
            html_to_text(offer.get("description")), html_to_text(offer.get("requirements"))
        ),
        url=text(offer.get("careers_url")) or f"{careers_url(board)}o/{offer.get('slug')}",
        apply_url=text(offer.get("careers_apply_url")) or None,
        location=location or None,
        remote=infer_remote(workplace, None, location),
        compensation=money(
            salary.get("min"), salary.get("max"), salary.get("currency"), salary.get("period")
        )
        or None,
        posted_at=iso(offer.get("published_at")),
        updated_at=iso(offer.get("updated_at")),
        source=NAME,
        source_id=text(offer.get("id")),
    )


def fetch(http, board, horizon_days=None):
    payload = require(http.json(API.format(token=board.token), missing_ok=True), dict)
    # Unclaimed subdomains serve demo offers whose slugs or URLs contain "sample".
    offers = [
        o
        for o in payload.get("offers") or []
        if isinstance(o, dict)
        and o.get("id")
        and "sample" not in f"{o.get('careers_url', '')} {o.get('slug', '')}".lower()
    ]
    name = next((text(o.get("company_name")) for o in offers if o.get("company_name")), None)
    return Listing([posting(board, o) for o in offers], name, len(offers))


probe = fetch
