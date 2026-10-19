import pytest
from pydantic import ValidationError
from test_evaluator import FakeProvider

from job_matcher.models import Profile
from job_matcher.questions import questions
from job_matcher.service import Service


def answer(topic="ai_development", statement="I use AI coding assistants and review their output."):
    return {"topic": topic, "status": "reported", "statement": statement, "user_confirmed": True}


def test_legacy_profile_and_resumable_interview(tmp_path):
    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": "Ten years managing engineering teams"})
    plan = service.onboarding()
    assert plan["ready"] and not plan["interview"]["complete"]
    assert len(plan["interview"]["pending_questions"]) == 9
    service.save_experience_answers([answer()])
    resumed = Service(tmp_path, FakeProvider()).onboarding()
    assert "ai_development" in resumed["interview"]["answered_topics"]
    assert len(resumed["interview"]["pending_questions"]) == 8


def test_merge_preserves_profile_and_replaces_only_answered_topic(tmp_path):
    service = Service(tmp_path, FakeProvider())
    service.save_profile(
        {"resume_text": "Resume text", "salary_floor": 210000, "preferences": ["Small teams"]}
    )
    service.save_experience_answers(
        [answer(), answer("developer_tooling", "I led CI/CD improvements.")]
    )
    service.save_experience_answers([answer(statement="I use AI for tests and implementation.")])
    saved = service.onboarding()["profile"]
    assert saved["resume_text"] == "Resume text"
    assert saved["salary_floor"] == 210000 and saved["preferences"] == ["Small teams"]
    assert len(saved["experience_answers"]) == 2
    facts = {a["topic"]: a["statement"] for a in saved["experience_answers"]}
    assert facts["ai_development"] == "I use AI for tests and implementation."
    assert facts["developer_tooling"] == "I led CI/CD improvements."


def test_unconfirmed_or_invalid_answers_do_not_partially_write(tmp_path):
    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": "Resume text", "target_roles": ["Engineering Manager"]})
    for invalid in [
        {**answer(), "user_confirmed": False},
        {k: v for k, v in answer().items() if k != "user_confirmed"},
        {**answer(), "statement": ""},
        {**answer(), "status": "skipped"},
    ]:
        with pytest.raises(ValidationError):
            service.save_experience_answers([answer("developer_tooling"), invalid])
        assert service.onboarding()["profile"]["experience_answers"] == []
    with pytest.raises(ValueError, match="unique"):
        service.save_experience_answers([answer(), answer()])


def test_skipped_and_no_experience_are_distinct(tmp_path):
    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": "Resume text", "target_roles": ["Engineering Manager"]})
    plan = service.onboarding()["interview"]
    service.save_experience_answers(
        [
            {"topic": q["topic"], "status": "skipped", "user_confirmed": True}
            for q in plan["pending_questions"]
        ]
    )
    plan = service.onboarding()["interview"]
    assert plan["complete"] and len(plan["revisit_topics"]) == 9
    service.save_experience_answers(
        [{"topic": "ai_products", "status": "no_experience", "user_confirmed": True}]
    )
    assert "ai_products" not in service.onboarding()["interview"]["revisit_topics"]


def test_profile_answers_invalidate_cache_and_preserve_job_status(tmp_path):
    provider = FakeProvider()
    service = Service(tmp_path, provider)
    service.save_profile({"resume_text": "Resume text", "target_roles": ["Engineering Manager"]})
    service.record_consent(True)
    job = {
        "title": "Manager",
        "company": "Example",
        "description": "Manage engineers remotely with a salary of $250000.",
    }
    first = service.evaluate(job)
    service.status(first["job_id"], "applied")
    assert service.evaluate(job)["cache_hit"]
    service.save_experience_answers([answer()])
    updated = service.evaluate(job)
    assert not updated["cache_hit"] and updated["status"] == "applied"
    assert updated["evaluation"]["profile_hash"] != first["evaluation"]["profile_hash"]
    assert all(
        "experience_answers" in questions(Profile())[key]["instructions"]
        for key in ["leadership", "technical", "seniority"]
    )


def test_cli_interview_answer_roundtrip(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": "Engineering leadership resume"})
    payload = tmp_path / "answers.json"
    payload.write_text(json.dumps([answer()]))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "job_matcher.cli",
            "--data-dir",
            str(tmp_path),
            "experience-save",
            "--input",
            str(payload),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ai_development" in json.loads(result.stdout)["interview"]["answered_topics"]
    assert service.onboarding()["profile"]["resume_text"] == "Engineering leadership resume"


def test_evaluation_requires_recorded_consent(tmp_path):
    from job_matcher import consent

    provider = FakeProvider()
    service = Service(tmp_path, provider)
    service.save_profile({"resume_text": "Resume text", "target_roles": ["Engineering Manager"]})
    job = {"title": "Manager", "company": "Example", "description": "Manage engineers. " * 3}
    status = service.onboarding()["consent"]
    assert not status["granted"] and "TypeSafe" in status["disclosure"]
    with pytest.raises(consent.ConsentRequired, match="record_consent"):
        service.evaluate(job)
    with pytest.raises(consent.ConsentRequired):
        service.batch([job])
    assert provider.calls == 0 and service.history() == []
    with pytest.raises(ValueError):
        service.record_consent(False)

    granted = service.record_consent(True)
    assert granted["granted"] and granted["record"]["version"] == consent.VERSION
    assert service.evaluate(job)["consent"]["granted_at"] == granted["record"]["granted_at"]
    assert "consent" not in service.onboarding()["missing"]
    # Consent is not part of the evidence sent to Jev, so recording it keeps the cache.
    assert service.evaluate(job)["cache_hit"]

    assert service.revoke_consent()["revoked"]
    with pytest.raises(consent.ConsentRequired):
        service.evaluate(job)
    assert service.history()  # local data survives revocation


def test_changed_disclosure_requires_renewal(tmp_path, monkeypatch):
    from job_matcher import consent

    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": "Resume text", "target_roles": ["Engineering Manager"]})
    service.record_consent(True)
    monkeypatch.setattr(consent, "VERSION", "2")
    assert service.consent()["needs_renewal"]
    with pytest.raises(consent.ConsentRequired, match="changed"):
        service.evaluate({"title": "M", "company": "E", "description": "Manage engineers. " * 3})
    assert service.record_consent(True)["granted"]


def test_cli_consent(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    def cli(*args, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), *args],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        )

    assert json.loads(cli("consent", "show").stdout)["granted"] is False
    declined = cli("consent", "grant", stdin="n\n")
    assert "TypeSafe" in declined.stderr and not json.loads(declined.stdout)["granted"]
    assert json.loads(cli("consent", "grant", stdin="yes\n").stdout)["granted"]
    assert json.loads(cli("consent", "revoke").stdout)["revoked"]
    assert json.loads(cli("consent", "grant", "--yes").stdout)["granted"]
