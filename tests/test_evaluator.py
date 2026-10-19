import copy
import json
import random
import subprocess
import sys

import pytest

from job_matcher.models import STRICTNESS, Job, Policy, Profile
from job_matcher.questions import questions
from job_matcher.scoring import summarize
from job_matcher.service import Service
from job_matcher.storage import identities


def response(overrides=None, strength=3.5):
    answers = {}
    good = {
        "role": "match",
        "remote": "compatible",
        "location": "compatible",
        "salary": "meets_target",
        "authorization": "compatible",
        "dealbreakers": "clear",
    }
    for key, q in questions().items():
        if q["type"] == "choice":
            label = (overrides or {}).get(key, good[key])
            answers[key] = {
                "type": "choice",
                "choice": label,
                "confidence": 0.9,
                "probabilities": {k: float(k == label) for k in q["criteria"]},
            }
        else:
            answers[key] = {
                "type": "score",
                "score": strength,
                "confidence": 0.9,
                "probabilities": {"0": 0, "1": 0, "2": 0, "3": 0.5, "4": 0.5},
            }
    return {"answers": answers, "model": "test-jev", "usage": {"input_tokens": 10}}


class FakeProvider:
    model = "test-jev"

    def __init__(self):
        self.calls = 0
        self.fail = False

    def evaluate(self, profile, job):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Provider unavailable")
        return response()


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path, FakeProvider())
    result.save_profile(
        Profile(
            resume_text="Managed teams of engineers for ten years.",
            target_roles=["Engineering Manager"],
        ).model_dump()
    )
    result.record_consent(True)
    return result


@pytest.fixture
def job():
    return {
        "title": "Engineering Manager",
        "company": "Acme",
        "description": "Manage a distributed team of engineers. Base salary $250,000.",
        "url": "https://www.linkedin.com/jobs/view/123456/?trk=abc",
    }


def test_happy_path_and_history(service, job):
    evaluated = service.evaluate(job)
    assert evaluated["evaluation"]["recommendation"] == "apply"
    assert evaluated["evaluation"]["fit_percent"] == 81.9
    assert len(service.shortlist()) == 1
    service.status(evaluated["job_id"], "surfaced")
    repeated = service.evaluate(job)
    assert repeated["cache_hit"]
    assert repeated["duplicate"]
    assert service.provider.calls == 1
    assert service.shortlist() == []
    assert service.batch([job])["shortlist"] == []


@pytest.mark.parametrize(
    "dimension,label",
    [
        ("salary", "below_floor"),
        ("remote", "incompatible"),
        ("location", "incompatible"),
        ("authorization", "incompatible"),
        ("role", "mismatch"),
        ("dealbreakers", "conflict"),
    ],
)
def test_hard_conflicts(dimension, label):
    # Salary is only a conflict when the profile states a floor; other dimensions always are.
    profile = (
        Profile(salary_floor=200000, salary_target=250000) if dimension == "salary" else Profile()
    )
    result = summarize(profile, response({dimension: label}))
    assert result["recommendation"] == "skip"
    assert dimension in result["dealbreakers"]


@pytest.mark.parametrize("dimension", ["remote", "dealbreakers"])
def test_unknown_consequential_is_investigate(dimension):
    result = summarize(Profile(), response({dimension: "unknown"}))
    assert result["recommendation"] == "investigate"
    assert not result["dealbreakers"]


@pytest.mark.parametrize("dimension", ["salary", "location", "authorization", "role"])
def test_unstated_detail_is_a_check_not_a_downgrade(dimension):
    profile = (
        Profile(salary_floor=200000, salary_target=250000) if dimension == "salary" else Profile()
    )
    result = summarize(profile, response({dimension: "unknown"}))
    assert result["recommendation"] == "apply"
    assert result["checks"] == [f"{dimension}: not stated in the posting"]


def test_salary_overlapping_floor_applies_with_check():
    profile = Profile(salary_floor=200000, salary_target=250000)
    result = summarize(profile, response({"salary": "overlaps_floor"}))
    assert result["recommendation"] == "apply"
    assert "confirm the offer" in result["checks"][0]


def test_salary_is_neutral_without_a_requirement():
    result = summarize(Profile(), response({"salary": "below_floor"}))
    assert result["recommendation"] == "apply"
    assert result["checks"] == []
    with_floor = summarize(
        Profile(salary_floor=1, salary_target=2), response({"salary": "below_floor"})
    )
    assert with_floor["recommendation"] == "skip"


def test_material_conflict_risk_is_investigate():
    raw = response()
    raw["answers"]["location"]["probabilities"] = {
        "compatible": 0.46,
        "incompatible": 0.44,
        "unknown": 0.1,
    }
    result = summarize(Profile(), raw)
    assert result["recommendation"] == "investigate"
    assert result["investigate"] == ["location: possible conflict (44% likely)"]


def test_rating_split_across_adjacent_levels_is_settled():
    # Jev confidence is low because the top level holds only 56%, but 93% of the
    # mass says strong or very strong: that should not trigger investigation.
    raw = response()
    raw["answers"]["leadership"]["confidence"] = 0.58
    raw["answers"]["leadership"]["probabilities"] = {
        "0": 0,
        "1": 0.01,
        "2": 0.06,
        "3": 0.37,
        "4": 0.56,
    }
    assert summarize(Profile(), raw)["recommendation"] == "apply"


def test_rating_spread_across_levels_is_investigate():
    raw = response()
    raw["answers"]["seniority"]["probabilities"] = {
        "0": 0.05,
        "1": 0.3,
        "2": 0.25,
        "3": 0.3,
        "4": 0.1,
    }
    result = summarize(Profile(), raw)
    assert result["recommendation"] == "investigate"
    assert result["investigate"] == ["seniority: rating uncertain"]


def test_stored_evaluations_follow_current_scoring(service, job):
    evaluated = service.evaluate(job)
    stale = {**evaluated["evaluation"], "recommendation": "investigate", "checks": None}
    with service.store.connect() as db:
        db.execute("UPDATE evaluations SET result=?", (json.dumps(stale),))
    assert service.history()[0]["evaluation"]["recommendation"] == "apply"
    assert service.evaluate(job)["evaluation"]["recommendation"] == "apply"
    assert service.provider.calls == 1


def test_low_confidence_conflict_does_not_reject():
    raw = response({"remote": "incompatible"})
    raw["answers"]["remote"]["confidence"] = 0.2
    assert summarize(Profile(), raw)["recommendation"] == "investigate"


def test_stretch_and_weak_matches():
    assert summarize(Profile(), response(strength=2.4))["stretch"]
    assert summarize(Profile(), response(strength=2.4))["recommendation"] == "apply"
    assert summarize(Profile(), response(strength=1))["recommendation"] == "skip"
    profile = Profile(policy={"allow_stretch": False})
    assert summarize(profile, response(strength=2.4))["recommendation"] == "skip"


def test_invalid_api_response_is_not_stored(service, job):
    raw = response()
    raw["answers"]["leadership"]["score"] = float("nan")
    service.provider.evaluate = lambda *args: raw
    with pytest.raises(ValueError):
        service.evaluate(job)
    assert service.history()[0]["evaluation"] is None


def test_missing_answers_rejected():
    raw = response()
    del raw["answers"]["salary"]
    with pytest.raises(ValueError):
        summarize(Profile(), raw)


def test_profile_change_and_job_change_invalidate_cache(service, job):
    first = service.evaluate(job)
    profile = service.onboarding()["profile"]
    profile["salary_floor"] = 220000
    service.save_profile(profile)
    assert service.shortlist() == []
    assert not service.evaluate(job)["cache_hit"]
    changed = {**job, "description": job["description"] + " New responsibilities."}
    assert service.evaluate(changed)["job_id"] == first["job_id"]
    assert service.provider.calls == 3


def test_force_bypasses_cache(service, job):
    service.evaluate(job)
    service.evaluate(job, force=True)
    assert service.provider.calls == 2


def test_applied_survives_profile_changes(service, job):
    first = service.evaluate(job)
    service.status(first["job_id"], "applied")
    profile = service.onboarding()["profile"]
    profile["preferences"] = ["Developer tools"]
    service.save_profile(profile)
    assert service.batch([job])["shortlist"] == []
    assert service.history()[0]["status"] == "applied"


def test_dedup_by_content_and_distinct_roles(service, job):
    first = service.evaluate(job)
    copied = {**job, "url": "https://acme.example/jobs/42"}
    assert service.evaluate(copied)["job_id"] == first["job_id"]
    distinct = {
        **job,
        "url": "https://acme.example/jobs/43",
        "description": job["description"] + " Lead a different department.",
    }
    assert service.evaluate(distinct)["job_id"] != first["job_id"]


def test_linkedin_url_aliases(job):
    one = identities(Job.model_validate(job))
    two = identities(
        Job.model_validate(
            {**job, "url": "https://www.linkedin.com/jobs/view/engineering-manager-123456/"}
        )
    )
    assert one[0] == two[0] == "linkedin:123456"


def test_batch_partial_errors_and_unique_shortlist(service, job):
    result = service.batch([job, {"title": "Bad"}, copy.deepcopy(job)])
    assert len(result["shortlist"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1


def test_provider_failure_can_retry(service, job):
    service.provider.fail = True
    with pytest.raises(RuntimeError):
        service.evaluate(job)
    service.provider.fail = False
    assert not service.evaluate(job)["cache_hit"]


def test_onboarding_requires_resume(tmp_path, job):
    service = Service(tmp_path, FakeProvider())
    assert service.onboarding()["missing"] == ["resume_text", "target_roles", "consent"]
    service.record_consent(True)
    service.save_profile(Profile().model_dump())
    with pytest.raises(ValueError, match="resume_text"):
        service.evaluate(job)
    ready = dict(Profile().model_dump(), resume_text="Ten years leading engineers.")
    service.save_profile(ready)
    with pytest.raises(ValueError, match="target_roles"):
        service.evaluate(job)
    service.save_profile(dict(ready, target_roles=["Engineering Manager"]))
    assert service.evaluate(job)["job_id"]


def test_cli_json_and_errors(tmp_path):
    command = [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path)]
    result = subprocess.run(command + ["onboarding"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout)["ready"] is False
    result = subprocess.run(
        command + ["profile-save"], input='{"salary_floor":-1}', capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "error" in json.loads(result.stdout)


def test_tracking_url_change_is_cached(service, job):
    first = service.evaluate(job)
    changed = {**job, "url": "https://www.linkedin.com/jobs/view/123456/?trk=new"}
    result = service.evaluate(changed)
    assert result["job_id"] == first["job_id"]
    assert result["cache_hit"]
    assert service.provider.calls == 1


def test_changed_job_failed_evaluation_does_not_surface_stale_result(service, job):
    service.evaluate(job)
    service.provider.fail = True
    changed = {**job, "description": job["description"] + " Role now requires office attendance."}
    with pytest.raises(RuntimeError):
        service.evaluate(changed)
    assert service.shortlist() == []


def test_validation_errors_do_not_echo_profile_data(tmp_path):
    command = [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), "profile-save"]
    payload = {"resume_text": "PRIVATE RESUME CONTENT", "salary_floor": -1}
    result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True)
    assert "PRIVATE RESUME CONTENT" not in result.stdout
    assert result.returncode == 1


def random_response(rng):
    raw = response()
    for key, q in questions().items():
        weights = [rng.random() ** 3 for _ in q["criteria"]]
        total = sum(weights)
        labels = list(q["criteria"]) if q["type"] == "choice" else [str(i) for i in range(5)]
        probabilities = {label: w / total for label, w in zip(labels, weights)}
        top = max(probabilities, key=probabilities.get)
        answer = raw["answers"][key]
        answer["probabilities"] = probabilities
        answer["confidence"] = probabilities[top]
        if q["type"] == "choice":
            answer["choice"] = top
        else:
            answer["score"] = sum(int(k) * v for k, v in probabilities.items())
    return raw


def test_stricter_stops_never_upgrade_a_recommendation():
    rank = {"apply": 2, "investigate": 1, "skip": 0}
    rng = random.Random(7)
    counts = dict.fromkeys(STRICTNESS, 0)
    for _ in range(2000):
        raw = random_response(rng)
        ranks = []
        for stop in STRICTNESS:
            result = summarize(Profile(policy={"strictness": stop}), raw)
            ranks.append(rank[result["recommendation"]])
            counts[stop] += result["recommendation"] == "apply"
        assert ranks == sorted(ranks, reverse=True)
    applies = list(counts.values())
    assert applies == sorted(applies, reverse=True) and applies[0] > applies[-1]


def test_doubt_destination_follows_strictness():
    raw = response({"remote": "unknown"})
    outcomes = {
        stop: summarize(Profile(policy={"strictness": stop}), raw)["recommendation"]
        for stop in STRICTNESS
    }
    assert outcomes == {
        "wide_net": "apply",
        "generous": "apply",
        "balanced": "investigate",
        "selective": "investigate",
        "strict": "skip",
    }


def test_confirmed_conflict_skips_at_every_stop():
    for stop in STRICTNESS:
        result = summarize(
            Profile(policy={"strictness": stop}), response({"remote": "incompatible"})
        )
        assert result["recommendation"] == "skip"


def test_unstated_details_count_as_doubt_only_when_selective():
    raw = response({"salary": "unknown"})
    profile = Profile(salary_floor=200000, salary_target=250000, policy={"strictness": "balanced"})
    assert summarize(profile, raw)["recommendation"] == "apply"
    selective = summarize(
        Profile(salary_floor=200000, salary_target=250000, policy={"strictness": "selective"}),
        raw,
    )
    assert selective["recommendation"] == "investigate"


def test_legacy_policy_migrates_to_balanced():
    legacy = {
        "apply_threshold": 70,
        "stretch_threshold": 50,
        "confidence_threshold": 0.65,
        "allow_stretch": True,
    }
    assert Policy.model_validate(legacy) == Policy()
    custom = Policy.model_validate({"confidence_threshold": 0.8, "allow_stretch": False})
    assert custom.strictness == "balanced"
    assert custom.resolved()["conflict_confidence"] == 0.8
    assert custom.resolved()["stretch_threshold"] == custom.resolved()["apply_threshold"]


def test_overrides_adjust_a_single_knob():
    policy = Policy(strictness="generous", apply_threshold=80)
    assert policy.resolved()["apply_threshold"] == 80
    assert policy.resolved()["stretch_threshold"] == STRICTNESS["generous"]["stretch_threshold"]
    with pytest.raises(ValueError):
        Policy(strictness="strict", apply_threshold=60)


def test_changing_strictness_is_free_and_immediate(service, job):
    raw = response({"salary": "unknown"})
    service.provider.evaluate = lambda *args: (setattr(service.provider, "calls", 1), raw)[1]
    service.save_profile(
        {
            "resume_text": "Managed teams of engineers for ten years.",
            "target_roles": ["Engineering Manager"],
            "salary_floor": 200000,
            "salary_target": 250000,
        }
    )
    first = service.evaluate(job)
    assert first["evaluation"]["recommendation"] == "apply"
    service.set_strictness("selective")
    assert service.history()[0]["evaluation"]["recommendation"] == "investigate"
    assert service.shortlist()[0]["evaluation"]["strictness"] == "selective"
    assert service.evaluate(job)["cache_hit"]
    service.set_strictness("strict")
    assert service.shortlist() == []
    assert service.onboarding()["interview"]["strictness"]["current"] == "strict"


def test_job_board_tracking_params_do_not_split_identity():
    base = "https://job-boards.greenhouse.io/acme/jobs/8197746"
    keys = {
        k
        for url in (base, base + "?gh_src=linkedin", base + "?gh_jid=1&lever-source=x")
        for k in identities(Job(title="EM", company="Acme", description="x" * 30, url=url))
        if k.startswith("url:")
    }
    assert len(keys) == 2  # gh_jid identifies the posting; tracking params do not


def test_job_summary_is_compact(service, job):
    from job_matcher.views import job_summary

    evaluated = service.evaluate(job)
    summary = job_summary(service.history()[0])
    assert summary["job_id"] == evaluated["job_id"] and summary["recommendation"] == "apply"
    assert "description" not in summary and "answers" not in summary
    assert job_summary(evaluated)["fit_percent"] == evaluated["evaluation"]["fit_percent"]
    assert "recommendation" not in job_summary({"job_id": "x", "job": {"title": "T"}})
