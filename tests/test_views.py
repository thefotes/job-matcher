"""Tests for the pure view functions over hand-built row dicts."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from job_matcher.models import Policy
from job_matcher.storage import digest
from job_matcher.views import is_current, matches

PROFILE_HASH = "profile123"
_UNSET = object()
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def job_hash(title="Role", company="Acme", description="Do things well"):
    return digest({"title": title, "company": company, "description": description})


def row(
    job_id="j1",
    status="new",
    historical=False,
    evaluation=_UNSET,
    posted_at="2026-09-01T00:00:00+00:00",
    first_seen="2026-09-02T00:00:00+00:00",
    first_shown_at=None,
    last_shown_at=None,
    title="Role",
    description="Do things well",
):
    job = {
        "title": title,
        "company": "Acme",
        "description": description,
        "posted_at": posted_at,
    }
    if evaluation is _UNSET:
        evaluation = {
            "profile_hash": PROFILE_HASH,
            "job_hash": job_hash(title=title, description=description),
            "recommendation": "apply",
            "fit_percent": 80,
        }
    return {
        "job_id": job_id,
        "historical": historical,
        "status": status,
        "job": job,
        "first_seen": first_seen,
        "first_shown_at": first_shown_at,
        "last_shown_at": last_shown_at,
        "evaluation": evaluation,
    }


def view(rows, **kwargs):
    return matches(rows, PROFILE_HASH, now=NOW, **kwargs)


def test_new_sorted_by_fit_then_recency():
    a = row("a")
    a["evaluation"]["fit_percent"] = 80
    b = row("b")
    b["evaluation"]["fit_percent"] = 90
    c = row("c", posted_at="2026-09-10T00:00:00+00:00")
    c["evaluation"]["fit_percent"] = 80
    result = view([a, b, c])
    assert [r["job_id"] for r in result["new"]] == ["b", "c", "a"]
    assert result["open"] == []
    assert result["excluded"] == {
        "decided": 0,
        "closed": 0,
        "historical": 0,
        "stale": 0,
        "skip": 0,
        "not_evaluated": 0,
    }


def test_open_sorted_by_fit_then_last_shown():
    low = row(
        "low",
        status="shown",
        first_shown_at="2026-09-20T00:00:00+00:00",
        last_shown_at="2026-09-20T00:00:00+00:00",
    )
    low["evaluation"]["fit_percent"] = 70
    old_high = row(
        "old_high",
        status="shown",
        first_shown_at="2026-09-25T00:00:00+00:00",
        last_shown_at="2026-09-25T00:00:00+00:00",
    )
    old_high["evaluation"]["fit_percent"] = 90
    recent_high = row(
        "recent_high",
        status="shown",
        first_shown_at="2026-09-28T00:00:00+00:00",
        last_shown_at="2026-09-28T00:00:00+00:00",
    )
    recent_high["evaluation"]["fit_percent"] = 90
    result = view([low, old_high, recent_high])
    assert [r["job_id"] for r in result["open"]] == ["recent_high", "old_high", "low"]


def test_each_exclusion_reason_counted_once():
    rows = [
        row("hist", historical=True),
        row("closed", status="closed"),
        row("applied", status="applied"),
        row("dismissed", status="dismissed"),
        row("noeval", evaluation=None),
        row(
            "oldprofile",
            evaluation={
                "profile_hash": "other",
                "job_hash": job_hash(),
                "recommendation": "apply",
                "fit_percent": 80,
            },
        ),
        row(
            "skip",
            evaluation={
                "profile_hash": PROFILE_HASH,
                "job_hash": job_hash(),
                "recommendation": "skip",
                "fit_percent": 80,
            },
        ),
        row(
            "shown",
            status="shown",
            first_shown_at="2026-08-01T00:00:00+00:00",
            last_shown_at="2026-08-01T00:00:00+00:00",
        ),
    ]
    result = view(rows)
    assert result["new"] == []
    assert result["open"] == []
    assert result["excluded"] == {
        "decided": 2,
        "closed": 1,
        "historical": 1,
        "stale": 1,
        "skip": 1,
        "not_evaluated": 2,
    }
    assert sum(result["excluded"].values()) == len(rows)


def test_thirty_day_window_and_include_stale():
    fresh = row(
        "fresh",
        status="shown",
        first_shown_at="2026-09-20T00:00:00+00:00",
        last_shown_at="2026-09-20T00:00:00+00:00",
    )
    stale = row(
        "stale",
        status="shown",
        first_shown_at="2026-08-15T00:00:00+00:00",
        last_shown_at="2026-08-15T00:00:00+00:00",
    )
    exactly = row(
        "exactly",
        status="shown",
        first_shown_at="2026-09-01T00:00:00+00:00",
        last_shown_at="2026-09-01T00:00:00+00:00",
    )
    assert [r["job_id"] for r in view([stale, fresh, exactly])["open"]] == ["fresh", "exactly"]
    assert view([stale, fresh, exactly])["excluded"]["stale"] == 1
    kept = view([stale, fresh, exactly], include_stale=True)
    assert [r["job_id"] for r in kept["open"]] == ["fresh", "exactly", "stale"]
    assert kept["excluded"]["stale"] == 0
    assert view([stale, fresh, exactly], open_window_days=60)["excluded"]["stale"] == 0


def test_missing_first_shown_at_is_not_stale():
    shown = row("shown", status="shown", first_shown_at=None, last_shown_at=None)
    result = view([shown])
    assert [r["job_id"] for r in result["open"]] == ["shown"]
    assert result["excluded"]["stale"] == 0


def test_is_current():
    good = row("good")
    assert is_current(good, PROFILE_HASH) is True
    changed = row(
        "changed",
        description="Now the text is different",
        evaluation={
            "profile_hash": PROFILE_HASH,
            "job_hash": job_hash(),
            "recommendation": "apply",
            "fit_percent": 80,
        },
    )
    assert is_current(changed, PROFILE_HASH) is False
    other = row(
        "other",
        evaluation={
            "profile_hash": "other",
            "job_hash": job_hash(),
            "recommendation": "apply",
            "fit_percent": 80,
        },
    )
    assert is_current(other, PROFILE_HASH) is False
    assert is_current(row("none", evaluation=None), PROFILE_HASH) is False


def test_policy_open_window_days_and_legacy():
    policy = Policy()
    assert policy.open_window_days == 30
    assert Policy(open_window_days=14).open_window_days == 14
    assert "open_window_days" not in Policy().resolved()
    with pytest.raises(ValidationError):
        Policy(open_window_days=0)
    with pytest.raises(ValidationError):
        Policy(open_window_days=366)
    legacy = {"apply_threshold": 65}
    assert Policy.model_validate(legacy).open_window_days == 30
