"""Matching states (docs/design-matching-states.md): the migration from stored statuses,
the derived row contract and the write semantics."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from job_matcher.models import Job
from job_matcher.service import Service
from job_matcher.storage import Store

# The jobs table as it was before matching states, with the columns earlier ALTERs added.
OLD_SCHEMA = """
    CREATE TABLE profiles (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        historical INTEGER NOT NULL DEFAULT 0, status_origin TEXT NOT NULL DEFAULT 'user');
    CREATE TABLE identities (key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id));
    CREATE TABLE evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
        cache_key TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE consents (
        provider TEXT PRIMARY KEY, version TEXT NOT NULL,
        disclosure TEXT NOT NULL, granted_at TEXT NOT NULL);
"""
OLD_ROWS = [
    # id, status, status_origin, historical
    ("surfaced", "surfaced", "user", 0),
    ("saved", "saved", "user", 0),
    ("applied-import", "applied", "import", 1),
    ("applied-user", "applied", "user", 0),
    ("interview", "interview", "import", 1),
    ("in-progress", "in_progress", "auto", 1),
    ("dismissed", "dismissed", "user", 0),
    ("closed", "closed", "user", 0),
    ("new", "new", "auto", 0),
]
FIRST_SEEN, LAST_SEEN = "2026-08-01T00:00:00+00:00", "2026-09-15T00:00:00+00:00"


def old_database(directory):
    with closing(sqlite3.connect(directory / "history.sqlite3")) as db, db:
        db.executescript(OLD_SCHEMA)
        for job_id, status, origin, historical in OLD_ROWS:
            data = {"title": job_id, "company": "Acme", "tracker_stage": None}
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                (job_id, json.dumps(data), status, FIRST_SEEN, LAST_SEEN, historical, origin),
            )


def snapshot(store):
    with closing(store.connect()) as db:
        return (
            [tuple(r) for r in db.execute("SELECT * FROM jobs ORDER BY id")],
            [tuple(r) for r in db.execute("SELECT * FROM shown ORDER BY id")],
            db.execute("PRAGMA user_version").fetchone()[0],
        )


def raw_statuses(store):
    with closing(store.connect()) as db:
        return dict(db.execute("SELECT id, status FROM jobs").fetchall())


def test_migrates_old_statuses_in_place_once(tmp_path):
    old_database(tmp_path)
    store = Store(tmp_path)
    rows = {row["job_id"]: row for row in store.history(100)}
    facts = {
        job_id: (
            row["status"],
            row["decision"],
            row["decision_origin"],
            row["decided_at"],
            row["closed_at"],
            row["times_shown"],
            row["first_shown_at"],
        )
        for job_id, row in rows.items()
    }
    assert facts == {
        "surfaced": ("shown", None, None, None, None, 1, LAST_SEEN),
        "saved": ("shown", None, None, None, None, 1, LAST_SEEN),
        "applied-import": ("applied", "applied", "import", LAST_SEEN, None, 0, None),
        "applied-user": ("applied", "applied", "user", LAST_SEEN, None, 0, None),
        "interview": ("applied", "applied", "import", LAST_SEEN, None, 0, None),
        "in-progress": ("applied", "applied", "user", LAST_SEEN, None, 0, None),
        "dismissed": ("dismissed", "dismissed", "user", LAST_SEEN, None, 0, None),
        "closed": ("closed", None, None, None, LAST_SEEN, 0, None),
        "new": ("new", None, None, None, None, 0, None),
    }
    assert {job_id: row["state_since"] for job_id, row in rows.items()} == {
        job_id: FIRST_SEEN if job_id == "new" else LAST_SEEN for job_id in rows
    }
    assert rows["interview"]["historical"] and not rows["surfaced"]["historical"]
    # The status column is a cache of the derived state.
    assert raw_statuses(store) == {job_id: row["status"] for job_id, row in rows.items()}

    # Opening it again changes nothing.
    before = snapshot(store)
    assert before[2] == 1
    assert snapshot(Store(tmp_path)) == before


def test_new_database_starts_migrated(tmp_path):
    store = Store(tmp_path)
    assert snapshot(store) == ([], [], 1)


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path)


def add(service, number=1):
    job = Job(
        title="Engineering Manager",
        company=f"Acme {number}",
        description="Manage a remote team of engineers.",
        source="board",
        source_id=str(number),
    )
    return service.store.ingest(job)[0]


def row(service, job_id):
    found = next(r for r in service.store.history(100) if r["job_id"] == job_id)
    assert raw_statuses(service.store)[job_id] == found["status"]  # cache in sync
    return found


def test_new_and_shown_are_derived_from_the_show_log(service):
    job_id = add(service)
    fresh = row(service, job_id)
    assert fresh["status"] == "new" and fresh["times_shown"] == 0
    assert fresh["first_shown_at"] is fresh["last_shown_at"] is None
    assert fresh["state_since"] == fresh["first_seen"]

    assert service.status(job_id, "shown") == {"job_id": job_id, "status": "shown"}
    service.status(job_id, "shown")
    shown = row(service, job_id)
    assert shown["status"] == "shown" and shown["times_shown"] == 2
    assert shown["first_shown_at"] <= shown["last_shown_at"]
    assert shown["state_since"] == shown["first_shown_at"]


def test_shown_never_changes_a_decision(service):
    applied, dismissed = add(service, 1), add(service, 2)
    service.status(applied, "applied")
    service.status(dismissed, "dismissed", reason="Not remote")
    before = row(service, applied)
    for job_id, status in [(applied, "applied"), (dismissed, "dismissed")]:
        assert service.status(job_id, "shown")["status"] == status
    after = row(service, applied)
    assert after["times_shown"] == 1
    assert (after["decision"], after["decided_at"]) == (before["decision"], before["decided_at"])
    assert row(service, dismissed)["decision_reason"] == "Not remote"


def test_decisions_record_origin_time_and_reason(service):
    job_id = add(service)
    service.status(job_id, "dismissed", reason="  Too junior  ")
    dismissed = row(service, job_id)
    assert dismissed["status"] == "dismissed"
    assert (dismissed["decision_origin"], dismissed["decision_reason"]) == ("user", "Too junior")
    assert dismissed["state_since"] == dismissed["decided_at"]

    service.status(job_id, "dismissed")  # repeating a decision keeps when it was made
    assert row(service, job_id)["decided_at"] == dismissed["decided_at"]

    service.status(job_id, "applied")
    applied = row(service, job_id)
    assert (applied["decision"], applied["decision_reason"]) == ("applied", None)
    with pytest.raises(ValueError, match="reason"):
        service.status(job_id, "applied", reason="Great fit")
    with pytest.raises(ValueError, match="reason"):
        service.statuses([job_id], "shown", reason="Seen")


def test_legacy_aliases(service):
    job_id = add(service)
    for alias, state in [
        ("surfaced", "shown"),
        ("saved", "shown"),
        ("interview", "applied"),
        ("in_progress", "applied"),
    ]:
        service.status(job_id, "new")
        assert service.status(job_id, alias)["status"] == state
        assert service.statuses([job_id], alias)["status"] == state
    with pytest.raises(ValueError):
        service.status(job_id, "archived")


def test_new_resets_decision_closure_and_showings(service):
    job_id = add(service)
    service.status(job_id, "shown")
    service.status(job_id, "dismissed", reason="Not now")
    service.status(job_id, "closed")
    assert service.status(job_id, "new")["status"] == "new"
    reset = row(service, job_id)
    assert reset["times_shown"] == 0
    assert all(
        reset[key] is None
        for key in ("decision", "decision_origin", "decision_reason", "decided_at", "closed_at")
    )
    assert reset["state_since"] == reset["first_seen"]


def test_closed_and_reopened(service):
    job_id = add(service)
    service.status(job_id, "shown")
    service.status(job_id, "applied")
    # The decision wins: an application stays "applied" after the posting comes down.
    assert service.store.set_closed(job_id, True) == "applied"
    closed = row(service, job_id)
    assert closed["decision"] == "applied" and closed["closed_at"]
    assert closed["state_since"] == closed["decided_at"]
    service.store.set_closed(job_id, True)
    assert row(service, job_id)["closed_at"] == closed["closed_at"]  # first time kept
    # The posting reappeared: reopening restores the state the facts give.
    assert service.store.set_closed(job_id, False) == "applied"
    assert row(service, job_id)["closed_at"] is None
    service.status(job_id, "new")
    service.status(job_id, "shown")
    service.status(job_id, "closed")
    assert service.store.set_closed(job_id, False) == "shown"
    with pytest.raises(ValueError, match="Unknown job_id"):
        service.store.set_closed("missing", True)


def test_record_shown_logs_known_jobs(service):
    first, second = add(service, 1), add(service, 2)
    service.status(second, "dismissed")
    assert service.store.record_shown([first, second, first, "missing"]) == [first, second]
    assert row(service, first)["status"] == "shown"
    assert row(service, second)["status"] == "dismissed"
    assert row(service, first)["times_shown"] == row(service, second)["times_shown"] == 1
    result = service.statuses([first, "missing"], "surfaced")
    assert result == {"status": "shown", "updated": [first], "unknown": ["missing"]}


def test_cli_status_aliases_and_reason(tmp_path):
    job_id = add(Service(tmp_path))

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), *args],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            text=True,
            capture_output=True,
        )
        return json.loads(result.stdout)

    assert cli("status", job_id, "surfaced")["status"] == "shown"
    assert cli("status", job_id, "dismissed", "--reason", "Office only")["status"] == "dismissed"
    assert Service(tmp_path).history()[0]["decision_reason"] == "Office only"
    assert "error" in cli("status", job_id, "applied", "--reason", "Nope")
