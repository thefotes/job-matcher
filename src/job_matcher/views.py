"""Compact views of stored jobs for agents: enough to rank, explain and act on a job,
without full descriptions or raw Jev answers. Tools return these by default; the full
records stay available on request."""

from datetime import datetime, timedelta, timezone

from job_matcher.sources.text import parse_date
from job_matcher.storage import digest

JOB_FIELDS = ("company", "title", "url", "apply_url", "posted_at", "location", "remote")
EVALUATION_FIELDS = (
    "recommendation",
    "fit_percent",
    "qualification_strength_percent",
    "stretch",
    "strictness",
    "dealbreakers",
    "investigate",
    "checks",
)


def job_summary(row):
    """Summarize a history, shortlist or evaluation row ({job_id, job, status, ...})."""
    job = row.get("job") or {}
    evaluation = row.get("evaluation") or {}
    summary = {"job_id": row.get("job_id"), "status": row.get("status")}
    summary |= {key: job.get(key) for key in JOB_FIELDS if job.get(key) is not None}
    if row.get("historical"):
        summary["historical"] = True
    if job.get("tracker_stage"):
        summary["tracker_stage"] = job["tracker_stage"]
    if row.get("times_shown"):
        summary["times_shown"] = row["times_shown"]
        summary["last_shown_at"] = row.get("last_shown_at")
    if evaluation:
        summary |= {key: evaluation.get(key) for key in EVALUATION_FIELDS}
        summary["evaluated_at"] = row.get("evaluated_at") or evaluation.get("evaluated_at")
    return summary


def _job_hash(data):
    return digest({key: data[key] for key in ("title", "company", "description")})


def is_current(row, profile_hash):
    """True if the row's evaluation was made for this profile and this exact job text."""
    evaluation = row.get("evaluation") or {}
    return bool(
        evaluation
        and evaluation.get("profile_hash") == profile_hash
        and evaluation.get("job_hash") == _job_hash(row["job"])
    )


def _parse(value):
    """UTC datetime, or None for missing or unparseable values (job boards vary)."""
    return parse_date(value)


def _stale(row, window, now):
    """A shown row is stale once its first showing predates the window. Missing
    first_shown_at means never timed, so it is kept."""
    if not row.get("first_shown_at"):
        return False
    first = _parse(row["first_shown_at"])
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return now - first > timedelta(days=window)


def matches(rows, profile_hash, open_window_days=30, now=None, include_stale=False):
    """New and still-open lists plus exclusion counts over full-detail history rows.

    Every row counts under exactly one reason, checked in order: historical, closed,
    decided, not_evaluated, skip, stale.
    """
    now = now or datetime.now(timezone.utc)
    excluded = {
        "decided": 0,
        "closed": 0,
        "historical": 0,
        "stale": 0,
        "skip": 0,
        "not_evaluated": 0,
    }
    new, open_rows = [], []
    for row in rows:
        if row.get("historical"):
            excluded["historical"] += 1
            continue
        status = row.get("status")
        if status == "closed":
            excluded["closed"] += 1
            continue
        if status in ("applied", "dismissed"):
            excluded["decided"] += 1
            continue
        if not is_current(row, profile_hash):
            excluded["not_evaluated"] += 1
            continue
        evaluation = row["evaluation"]
        if evaluation.get("recommendation") == "skip":
            excluded["skip"] += 1
            continue
        if status == "shown":
            if _stale(row, open_window_days, now):
                if include_stale:
                    open_rows.append(row)
                else:
                    excluded["stale"] += 1
                continue
            open_rows.append(row)
        elif status == "new":
            new.append(row)
        else:
            excluded["not_evaluated"] += 1

    def epoch(value):
        parsed = _parse(value)
        return parsed.timestamp() if parsed else 0.0

    new.sort(
        key=lambda r: (
            -r["evaluation"]["fit_percent"],
            -epoch(r["job"].get("posted_at") or r.get("first_seen")),
        )
    )
    open_rows.sort(
        key=lambda r: (
            -r["evaluation"]["fit_percent"],
            -epoch(r.get("last_shown_at")),
        )
    )
    return {"new": new, "open": open_rows, "excluded": excluded}
