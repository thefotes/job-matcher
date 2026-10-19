import json

import httpx2
import pytest
from typesafe_sdk import TypeSafeClient

from job_matcher.models import ExperienceAnswer, Job, Profile
from job_matcher.provider import JevProvider, ProviderError
from job_matcher.questions import questions
from job_matcher.scoring import summarize


def test_actual_sdk_http_contract(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-only-key"
        assert body["model"] == "jev-latest"
        assert body["state"]["candidate"]["resume_text"] == "Ten years leading engineering teams"
        assert (
            body["state"]["candidate"]["experience_answers"][0]["statement"]
            == "I use AI coding tools and review generated tests."
        )
        answers = {}
        for key, question in body["questions"].items():
            criteria = question["criteria"]
            if question["type"] == "choice":
                label = next(iter(criteria))
                answers[key] = {
                    "type": "choice",
                    "choice": label,
                    "confidence": 1,
                    "probabilities": {k: float(k == label) for k in criteria},
                }
            else:
                answers[key] = {
                    "type": "score",
                    "score": 3,
                    "confidence": 1,
                    "legend": {str(i): c for i, c in enumerate(criteria)},
                    "probabilities": {str(i): float(i == 3) for i in range(5)},
                }
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    monkeypatch.setattr(
        "job_matcher.provider.TypeSafeClient",
        lambda **kwargs: TypeSafeClient(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    profile = Profile(
        resume_text="Ten years leading engineering teams",
        experience_answers=[
            ExperienceAnswer(
                topic="ai_development",
                status="reported",
                statement="I use AI coding tools and review generated tests.",
                user_confirmed=True,
            )
        ],
    )
    job = Job(title="Manager", company="Example", description="Lead a product engineering team")
    raw = JevProvider().evaluate(profile, job)
    assert set(raw["answers"]) == set(questions())
    # The mock selects below_floor; with no salary requirement set, that is neutral.
    assert summarize(profile, raw)["recommendation"] == "apply"
    assert len(calls) == 1


def test_no_key_fails_without_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="TYPESAFE_API_KEY"):
        JevProvider().evaluate(Profile(), None)


def test_salary_question_uses_actual_profile_thresholds():
    profile = Profile(salary_floor=210000, salary_target=275000, salary_currency="CAD")
    question = questions(profile)["salary"]
    assert "210,000 CAD" in question["instructions"]
    assert "275,000 CAD" in question["criteria"]["meets_target"]
    assert "CAD" in question["criteria"]["unknown"]
