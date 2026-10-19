import json
import subprocess
import sys

import pytest
from test_evaluator import FakeProvider

from job_matcher.models import Job
from job_matcher.service import Service


def entry(**changes):
    return {
        "title": "Manager",
        "company": "Acme",
        "source": "linkedin",
        "source_id": "123",
        "tracker_stage": "applied",
        **changes,
    }


def test_no_profile_or_api_and_repeat(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    provider = FakeProvider()
    service = Service(tmp_path, provider)
    first = service.import_history([entry()])["results"][0]
    repeated = service.import_history([entry()])["results"][0]
    assert first["job_id"] == repeated["job_id"]
    assert repeated["duplicate"]
    row = service.history()[0]
    assert row["job"]["description"] is None
    assert row["evaluation"] is None
    assert row["historical"]
    assert provider.calls == 0
    assert service.shortlist() == []


def test_full_description_merge_and_user_status(tmp_path):
    service = Service(tmp_path, FakeProvider())
    imported = service.import_history([entry(tracker_stage="interview")])["results"][0]
    service.save_profile(
        {
            "resume_text": "Managed engineering teams for ten years.",
            "target_roles": ["Engineering Manager"],
        }
    )
    service.record_consent(True)
    job = {
        "title": "Manager",
        "company": "Acme",
        "description": "Manage engineers remotely with salary $250000.",
        "url": "https://www.linkedin.com/jobs/view/manager-123/?trk=abc",
    }
    evaluated = service.evaluate(job)
    assert evaluated["job_id"] == imported["job_id"]
    assert evaluated["status"] == "applied"
    service.status(imported["job_id"], "new")
    assert service.batch([job])["shortlist"] == []
    assert service.shortlist() == []
    service.status(imported["job_id"], "dismissed")
    service.import_history([entry(tracker_stage="in_progress")])
    row = service.history()[0]
    assert row["status"] == "dismissed"
    assert row["job"]["description"] == job["description"]
    assert row["job"]["tracker_stage"] == "in_progress"


def test_imported_stages_count_as_applied(tmp_path):
    service = Service(tmp_path)
    decided_at = None
    for stage in ["in_progress", "applied", "interview", "applied"]:
        result = service.import_history([entry(tracker_stage=stage)])
        assert result["results"][0]["status"] == "applied"
        assert result["results"][0]["tracker_stage"] == stage
        row = service.history()[0]
        assert row["job"]["tracker_stage"] == stage  # the latest observation, verbatim
        assert (row["decision"], row["decision_origin"]) == ("applied", "import")
        # The first import made the decision; later ones observe it.
        decided_at = decided_at or row["decided_at"]
        assert row["decided_at"] == decided_at == row["state_since"]


def test_partial_validation_and_identity_conflict(tmp_path):
    service = Service(tmp_path)
    result = service.import_history(
        [
            entry(),
            {},
            entry(source_id="456", url="https://linkedin.com/jobs/view/789/"),
            entry(source_id="456", tracker_stage="in_progress"),
        ]
    )
    assert [r["index"] for r in result["results"]] == [0, 3]
    assert [r["index"] for r in result["errors"]] == [1, 2]
    assert len(service.history()) == 2
    for value in [None, {}, [], [entry()] * 101]:
        with pytest.raises(ValueError):
            service.import_history(value)


def test_cli_partial_failure(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), "history-import"],
        input=json.dumps([entry(), {}]),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    body = json.loads(result.stdout)
    assert len(body["results"]) == len(body["errors"]) == 1


def test_imports_never_overwrite_user_decisions(tmp_path):
    service = Service(tmp_path)
    for number, (status, expected) in enumerate(
        [
            ("dismissed", "dismissed"),  # a user decision stands
            ("applied", "applied"),
            ("closed", "applied"),  # recorded; the decision wins over the closed posting
            ("shown", "applied"),  # showing is not a decision
            ("new", "applied"),  # a reset clears the decision, so the tracker sets it
        ]
    ):
        job_id = service.store.ingest(
            Job(
                title="Manager",
                company=f"Acme {number}",
                description="Manage a remote team of engineers.",
                source="linkedin",
                source_id=str(number),
            )
        )[0]
        service.status(job_id, status)
        imported = service.import_history(
            [entry(company=f"Acme {number}", source_id=str(number), tracker_stage="interview")]
        )
        assert imported["results"][0]["status"] == expected, status
        row = next(r for r in service.history() if r["job_id"] == job_id)
        if status in {"dismissed", "applied"}:
            assert row["decision_origin"] == "user"
        else:
            assert (row["decision"], row["decision_origin"]) == ("applied", "import")
    assert service.import_history([entry(url="https://")])["errors"]


def test_explicit_directory_loads_configured_environment(tmp_path, monkeypatch):
    environment_file = tmp_path / "configured.env"
    configured_directory = tmp_path / "environment-data"
    environment_file.write_text(
        f"TYPESAFE_DEFAULT_MODEL=test-env-model\nJOB_MATCHER_DATA_DIR={configured_directory}\n"
    )
    monkeypatch.setenv("JOB_MATCHER_ENV_FILE", str(environment_file))
    monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("JOB_MATCHER_DATA_DIR", raising=False)
    explicit_directory = tmp_path / "explicit-data"
    try:
        service = Service(explicit_directory)
        assert service.store.directory == explicit_directory.resolve()
        assert service.provider.model == "test-env-model"
        assert not configured_directory.exists()
        assert service.import_history([entry()])["results"][0]["status"] == "applied"
    finally:
        # dotenv writes to os.environ directly, outside monkeypatch tracking.
        monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
        monkeypatch.delenv("JOB_MATCHER_DATA_DIR", raising=False)
