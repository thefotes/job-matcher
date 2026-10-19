"""The daily routine in one call: discover, triage, evaluate, rank, check liveness.

Agents follow long prompts only partially; code runs the same way every time. Discovery
and triage never call TypeSafe. Evaluation needs recorded consent and stops at a cap.
Nothing is marked shown here: the agent does that after showing the jobs.
"""

from datetime import datetime, timedelta, timezone

from job_matcher.discovery import title_key
from job_matcher.errors import error_message
from job_matcher.models import Profile
from job_matcher.service import job_hash, rescore
from job_matcher.sources.resolve import same_company
from job_matcher.sources.text import parse_date, salary_ranges
from job_matcher.storage import digest
from job_matcher.views import job_summary, matches

DUPLICATE_WINDOW = timedelta(days=45)
HISTORY_WINDOW = 1000  # the same window get_shortlist searches
MAX_LISTED_ERRORS = 10
MAX_CONSECUTIVE_FAILURES = 3  # stop paying for a run once the provider is clearly down
SCORE_NOTE = "Fit and qualification are ranking scores, not hiring odds."


def _current(evaluation, profile_hash, data):
    """True if an evaluation was made for this profile and this exact job text."""
    return bool(
        evaluation
        and evaluation["profile_hash"] == profile_hash
        and evaluation.get("job_hash") == job_hash(data)
    )


def _recorded(row):
    """When a history record's opening was posted, else first seen."""
    return parse_date(row["job"].get("posted_at")) or parse_date(row["first_seen"])


def _duplicate_of(job, rows):
    """The newest history record from another source with the same company and normalized
    title, posted within DUPLICATE_WINDOW of this posting. A likely duplicate, not proof:
    one company can list two openings with the same title on the same board, so records
    from the posting's own source never match."""
    key = title_key(job["title"])
    posted = parse_date(job.get("posted_at")) or datetime.now(timezone.utc)
    source = (job.get("source") or "").casefold()
    matches = [
        row
        for row in rows
        if (row["job"].get("source") or "").casefold() != source
        and title_key(row["job"].get("title")) == key
        and same_company(row["job"].get("company"), job["company"])
        and abs(posted - _recorded(row)) <= DUPLICATE_WINDOW
    ]
    return max(matches, key=_recorded, default=None)


def _listings(store, reports):
    """Source IDs currently listed on each board read in this run (fetched or cached)."""
    read = {}
    for report in reports:
        listing = report["status"] != "error" and store.board_listing(report["board_id"])
        if listing:
            read[report["board_id"]] = (
                {str(p.get("source_id")) for p in listing["postings"]},
                listing["complete"],
            )
    return read


def _live(job, boards, read):
    """Whether a board posting is still listed: True, False, or None when unknown.

    Adapters stamp postings with their board's company, so a job belongs to the saved
    boards with its provider and company. Absence proves closure only when every such
    board was read in this run and its listing is complete (Workday stops paging at the
    date horizon, so older postings can be missing while still open).
    """
    source = (job.get("source") or "").casefold()
    owners = [
        b["id"]
        for b in boards
        if b["provider"] == source and same_company(b["company"], job.get("company"))
    ]
    if any(job.get("source_id") in read[b][0] for b in owners if b in read):
        return True
    if owners and all(b in read and read[b][1] for b in owners):
        return False
    return None


def _employer_from_boards(job, boards, read, store):
    """The one posting matching this job's title on the job company's boards read in this
    run, else None. Zero or several same-title postings are ambiguous: nothing is set."""
    if job.get("apply_url"):
        return None
    if any(b["provider"] == (job.get("source") or "").casefold() for b in boards):
        return None  # the job came from a job board: its url already is the employer link
    key = title_key(job["title"])
    urls = []
    for board in boards:
        if board["id"] not in read or not same_company(board["company"], job.get("company")):
            continue
        listing = store.board_listing(board["id"]) or {}
        urls += [
            p["url"]
            for p in listing.get("postings", [])
            if p.get("url") and title_key(p.get("title")) == key
        ]
    unique = list(dict.fromkeys(urls))
    return unique[0] if len(unique) == 1 else None


def _cell(text):
    return " ".join(str(text).split()).replace("|", "/")


def _link(label, url):
    return f"[{label}]({url})" if url else ""


def _salary(job):
    """The posting's pay as '$129k–$210k', spanning every range it lists (often pay zones
    by location, counted in the label); None when it states no dollar range."""
    ranges = salary_ranges(job.get("compensation")) or salary_ranges(job.get("description"))
    if not ranges:
        return None
    low, high = min(r[0] for r in ranges), max(r[1] for r in ranges)
    span = f"${low / 1000:.0f}k–${high / 1000:.0f}k"
    return span if len(ranges) == 1 else f"{span} ({len(ranges)} ranges)"


def _table(jobs):
    lines = ["| Role | Fit | Salary | Call | Links | Notes |", "|---|---:|---|---|---|---|"]
    for job in jobs:
        call = job.get("recommendation", "").capitalize()
        if job.get("stretch"):
            call += " (stretch)"
        if job.get("new_this_run"):
            call += " · just scored"
        url, employer = job.get("url"), job.get("employer_url") or job.get("apply_url")
        if employer == url:
            employer = None
        label = "LinkedIn" if url and "linkedin.com" in url else "Posting"
        links = " · ".join(x for x in (_link(label, url), _link("Employer", employer)) if x)
        notes = "; ".join((job.get("investigate") or []) + (job.get("checks") or []))
        lines.append(
            f"| {_cell(job['company'])} — {_cell(job['title'])} "
            f"| {job.get('fit_percent', 0):.0f}% | {job.get('salary') or '—'} "
            f"| {call} | {links or '—'} "
            f"| {_cell(notes) or '—'} |"
        )
    return lines


def report_markdown(new, still_open, coverage):
    """The user-facing report, built here so every client shows the same thing: New (never
    shown) and Still open (shown before, no decision yet). Agents put it in their final
    reply verbatim."""
    lines = ["**New since your last run**", ""]
    if new:
        lines += _table(new)
    else:
        lines.append(
            f"Nothing new: {coverage['discovered']} recent postings checked, and none that "
            "you haven't seen qualified."
        )
    lines += ["", "**Still open** (shown before, no decision yet)", ""]
    if still_open:
        lines += _table(still_open)
    else:
        lines.append("Nothing waiting on a decision.")
    lines += [
        "",
        f"{coverage['boards_read']} boards searched · {coverage['discovered']} recent postings · "
        f"{coverage['evaluated']} newly scored · strictness: {coverage['strictness']}. "
        + SCORE_NOTE,
    ]
    return "\n".join(lines)


def _brief(job):
    return {k: v for k, v in job_summary({"job": job}).items() if v is not None}


def run(service, since_days=7, max_new_evaluations=15, top_n=10, refresh=False, open_n=5):
    if not 0 <= max_new_evaluations <= 100:
        raise ValueError("max_new_evaluations must be 0–100")
    if not 1 <= top_n <= 50:
        raise ValueError("top_n must be 1–50")
    if not 0 <= open_n <= 50:
        raise ValueError("open_n must be 0–50")
    saved = service.store.profile()
    profile = Profile.model_validate(saved) if saved else None
    profile_hash = digest(profile.evidence()) if profile else None
    agreement = service.consent()
    missing = profile.missing_required() if profile else ["resume_text", "target_roles"]
    if missing:
        blocker = "onboarding_required"
    elif not agreement["granted"]:
        blocker = "consent_required"
    else:
        blocker = None

    # 1. Discover. find_jobs returns newest first and marks postings already in history.
    found = service.find_jobs(since_days=since_days, limit=100, refresh=refresh)

    # 2. Triage without paid calls.
    rows = service.store.history(HISTORY_WINDOW)
    by_id = {row["job_id"]: row for row in rows}
    by_title = {}
    for row in rows:
        by_title.setdefault(title_key(row["job"].get("title")), []).append(row)
    known, current, pending, duplicates = 0, 0, [], []
    for item in found["jobs"]:
        job, mark = item["job"], item["known"]
        if mark and (mark["status"] != "new" or mark["historical"]):
            known += 1
        elif mark:
            row = by_id.get(mark["job_id"])
            if row and _current(row["evaluation"], profile_hash, job):
                current += 1
            else:
                pending.append(job)  # unevaluated, or evaluated under an older profile
        elif original := _duplicate_of(job, by_title.get(title_key(job["title"]), [])):
            if original["job"].get("apply_url") == job.get("url"):
                known += 1  # linked to its record as the employer link on an earlier run
            else:
                duplicates.append((original, job))
        else:
            pending.append(job)

    # 3. Evaluate new postings, newest first, up to the cap; only with recorded consent.
    batch, deferred = pending[:max_new_evaluations], pending[max_new_evaluations:]
    evaluated, cache_hits, tokens, errors = set(), 0, 0, []
    failures, stopped_early = 0, False
    for index, job in enumerate([] if blocker else batch):
        try:
            result = service.evaluate(job)
        except (ValueError, RuntimeError) as exc:
            errors.append(
                {"company": job["company"], "title": job["title"], "error": error_message(exc)}
            )
            failures = failures + 1 if isinstance(exc, RuntimeError) else 0
            if failures >= MAX_CONSECUTIVE_FAILURES:
                deferred = batch[index + 1 :] + deferred
                stopped_early = True
                break
            continue
        failures = 0
        evaluated.add(result["job_id"])
        if result["cache_hit"]:
            cache_hits += 1
        else:
            tokens += (result["evaluation"].get("usage") or {}).get("input_tokens") or 0

    # 4. Liveness is a fact about the posting: record closures, and reopen postings that
    #    reappear. Decided jobs keep their decision; their closure is still recorded.
    boards, read = service.store.boards(), _listings(service.store, found["boards"])
    gone, reopened = [], []
    for row in service.store.history(HISTORY_WINDOW) if profile else []:
        live = _live(row["job"], boards, read)
        brief = {"job_id": row["job_id"], "company": row["job"].get("company")}
        brief |= {"title": row["job"].get("title"), "url": row["job"].get("url")}
        if live is False and not row["closed_at"]:
            service.store.set_closed(row["job_id"], True)
            if row["status"] in {"new", "shown"}:
                gone.append(brief)
        elif live is True and row["closed_at"]:
            service.store.set_closed(row["job_id"], False)
            reopened.append(brief)

    # 5. The New and Still open views, under the current profile and strictness.
    found_views = matches(
        service.history(HISTORY_WINDOW, detail=True) if profile else [],
        profile_hash,
        profile.policy.open_window_days if profile else 30,
    )
    employer = {}
    for original, job in duplicates:
        employer.setdefault(original["job_id"], job.get("url"))
        # Keep the employer link on the record so it outlives the board's search window.
        service.store.fill_job_fields(original["job_id"], {"apply_url": job.get("url")})

    def present(row):
        summary = job_summary(row)
        if employer.get(row["job_id"]):
            summary["employer_url"] = employer[row["job_id"]]
        elif url := _employer_from_boards(row["job"], boards, read, service.store):
            summary["employer_url"] = url
            service.store.fill_job_fields(row["job_id"], {"apply_url": url})
        if salary := _salary(row["job"]):
            summary["salary"] = salary
        live = _live(row["job"], boards, read)
        if live is not None:
            summary["live"] = live
        if row["job_id"] in evaluated:
            summary["new_this_run"] = True
        return summary

    new = [present(row) for row in found_views["new"][:top_n]]
    still_open = [present(row) for row in found_views["open"][:open_n]]

    # 6. Coverage.
    board_errors = found["errors"]
    coverage = {
        "since_days": since_days,
        "title_keywords": found["filters"]["title_keywords"],
        "remote_only": found["filters"]["remote_only"],
        "boards_read": len(read),
        "boards_failed": len(board_errors),
        "deactivated_boards": found.get("deactivated_boards", []),
        "board_errors": [
            {"company": e["company"], "error": e["error"]} for e in board_errors[:MAX_LISTED_ERRORS]
        ],
        "discovered": found.get("matched", 0),
        "skipped_known": known,
        "already_evaluated": current,
        "duplicates": len(duplicates),
        "evaluated": len(evaluated),
        "cache_hits": cache_hits,
        "evaluation_errors": errors,
        "deferred": len(deferred),
        "stopped_after_failures": stopped_early,
        "input_tokens": tokens,
        "strictness": profile.policy.strictness if profile else None,
        "new_matches": len(found_views["new"]),
        "open_matches": len(found_views["open"]),
        "open_window_days": profile.policy.open_window_days if profile else None,
        "excluded": found_views["excluded"],
        "gone": len(gone),
        "reopened": len(reopened),
    }
    if found.get("matched", 0) > found.get("returned", 0):
        coverage["not_read_beyond_limit"] = found["matched"] - found["returned"]

    steps = []
    if not found["boards"]:
        steps.append("No active job boards: add some with add_job_board or import_job_boards.")
    if blocker == "onboarding_required":
        steps.append(
            f"Nothing was evaluated: the profile is missing {', '.join(missing)}. Finish "
            "onboarding (get_onboarding), then run daily_run again."
        )
    elif blocker == "consent_required":
        steps.append(
            "Nothing was evaluated: evaluation sends the user's profile to TypeSafe and "
            + (
                "the disclosure changed since they consented. "
                if agreement["needs_renewal"]
                else "they have not consented. "
            )
            + "Show the user consent.disclosure verbatim and ask; only if they explicitly "
            "agree, call record_consent with user_confirmed=true, then run daily_run again. "
            "`unevaluated` lists the new postings; do not score them yourself."
        )
    steps.append(
        "Call set_job_status once with job_ids set to `shown_ids` and status shown (this "
        "only records that they were shown; nothing is hidden), then end with a final reply "
        "that contains report_markdown verbatim. Some clients collapse everything but the "
        "final message, so the report must be in it; do not replace it with a summary."
    )
    if duplicates:
        steps.append(
            "`duplicates` are postings that likely repeat a job already in history; they "
            "were not evaluated again."
        )
    if gone or reopened:
        steps.append(
            "`gone` jobs left their job boards and were recorded as closed; `reopened` ones "
            "came back. Mention them briefly."
        )
    if stopped_early:
        steps.append(
            f"Evaluation stopped after {MAX_CONSECUTIVE_FAILURES} provider errors in a row; "
            "report the error to the user and retry later."
        )
    if deferred and not blocker:
        steps.append(f"{len(deferred)} new postings exceeded the cap; the next run takes them.")

    result = {
        "status": blocker or "complete",
        "report_markdown": report_markdown(new, still_open, coverage),
        "new": new,
        "open": still_open,
        "shown_ids": [job["job_id"] for job in new + still_open],
        "duplicates": [
            job_summary({**original, "evaluation": rescore(profile, original["evaluation"])})
            | {"employer_url": job.get("url")}
            for original, job in duplicates
        ],
        "gone": gone,
        "reopened": reopened,
        "coverage": coverage,
        "score_note": SCORE_NOTE,
        "next_step": " ".join(steps),
    }
    if blocker:
        result["unevaluated"] = [_brief(job) for job in batch]
    if blocker == "consent_required":
        result["consent"] = agreement
    return result
