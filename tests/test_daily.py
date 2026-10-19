import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_discovery import ASHBY, GH, FakeAts, ago, ashby_payload, greenhouse_payload
from test_evaluator import FakeProvider, response

from job_matcher import consent
from job_matcher.daily import _duplicate_of
from job_matcher.discovery import Discovery
from job_matcher.service import Service
from job_matcher.sources.http import Http

ACME_12 = "https://job-boards.greenhouse.io/acme/jobs/12"
GLOBEX_A1 = "https://jobs.ashbyhq.com/globex/a-1"


class RankedProvider(FakeProvider):
    """Qualification strength by title so ranking is observable; titles in SKIP mismatch."""

    STRENGTH = {"Engineering Manager": 2.0, "Director of Engineering": 3.0}
    SKIP = {"Manager, Software Engineering"}

    def evaluate(self, profile, job):
        self.calls += 1
        overrides = {"role": "mismatch"} if job.title in self.SKIP else None
        return response(overrides, self.STRENGTH.get(job.title, 3.5))


@pytest.fixture
def ats():
    return FakeAts(
        {
            GH.format("acme"): greenhouse_payload(),
            GH.format("acme") + "?content=true": greenhouse_payload(),
            ASHBY.format("globex"): ashby_payload(),
        }
    )


@pytest.fixture
def service(tmp_path, ats):
    """Acme (Greenhouse) and Globex (Ashby) boards list three Engineering Manager roles
    from the past week: Acme 12 (1 day old), Acme 11 (2 days), Globex a-1 (3 days)."""
    result = Service(tmp_path, RankedProvider())
    result._discovery = Discovery(result.store, Http(ats, backoff=0))
    result.save_profile(
        {
            "resume_text": "Managed engineering teams for ten years.",
            "target_roles": ["Engineering Manager"],
            "remote_only": False,
            "location": "United States",
        }
    )
    result.add_job_board(careers_url="https://boards.greenhouse.io/acme")
    result.add_job_board("Globex", "https://jobs.ashbyhq.com/globex")
    return result


def linkedin_job(title, company, job_id):
    return {
        "title": title,
        "company": company,
        "description": f"{company} is hiring a {title} to lead a remote team. LinkedIn copy.",
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "source": "linkedin",
        "source_id": str(job_id),
    }


def board_job(service, source_id):
    found = service.find_jobs(title_keywords=["Engineering Manager"])["jobs"]
    return next(r["job"] for r in found if r["job"]["source_id"] == source_id)


def assert_compact(result):
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    assert not keys & {"description", "answers", "signals", "job_snapshot", "profile_hash"}


def listed(result):
    """Every job a daily_run reported: New first, then Still open."""
    return result["new"] + result["open"]


def statuses(service):
    return {row["job"]["title"]: row["status"] for row in service.store.history(100)}


def test_without_consent_discovers_but_never_evaluates(service):
    result = service.daily_run()
    assert result["status"] == "consent_required"
    assert service.provider.calls == 0
    assert [j["title"] for j in result["unevaluated"]] == [
        "Engineering Manager",
        "Senior Engineering Manager, Platform",
        "Manager, Software Engineering",
    ]
    assert result["unevaluated"][0]["url"] == ACME_12
    assert "job_id" not in result["unevaluated"][0]
    assert result["consent"]["granted"] is False
    assert result["consent"]["disclosure"] == consent.DISCLOSURE
    assert result["consent"]["record_with"] == "record_consent"
    assert "record_consent" in result["next_step"]
    assert "have not consented" in result["next_step"]
    coverage = result["coverage"]
    assert coverage["discovered"] == 3 and coverage["evaluated"] == 0
    assert coverage["boards_read"] == 2 and coverage["boards_failed"] == 0
    assert listed(result) == []
    assert service.store.history(100) == []  # discovery stores nothing in history
    assert_compact(result)

    service.store.save_consent(consent.PROVIDER, "0", "an older disclosure")
    renewal = service.daily_run(max_new_evaluations=1)
    assert renewal["status"] == "consent_required"
    assert "disclosure changed" in renewal["next_step"]
    assert len(renewal["unevaluated"]) == 1 and renewal["coverage"]["deferred"] == 2
    assert service.provider.calls == 0


def test_skips_known_jobs_and_reports_cross_source_duplicates(service):
    service.record_consent(True)
    # Applied on LinkedIn to the opening Acme also lists on its own board (Acme 12).
    service.import_history(
        [
            {
                "title": "Engineering Manager",
                "company": "Acme",
                "source": "linkedin",
                "source_id": "555",
                "url": "https://www.linkedin.com/jobs/view/555/",
                "tracker_stage": "applied",
            }
        ]
    )
    # Globex a-1 was already found and evaluated on LinkedIn under a reworded title.
    shortlisted = service.evaluate(
        linkedin_job("Software Engineering Manager", "Globex, Inc.", 777)
    )
    # Acme 11 was dismissed.
    dismissed = service.evaluate(board_job(service, "11"))
    service.status(dismissed["job_id"], "dismissed")
    calls = service.provider.calls

    result = service.daily_run()
    assert result["status"] == "complete"
    assert service.provider.calls == calls  # nothing new is evaluated
    coverage = result["coverage"]
    assert coverage["discovered"] == 3 and coverage["skipped_known"] == 1
    assert coverage["duplicates"] == 2 and coverage["evaluated"] == 0
    assert coverage["input_tokens"] == 0

    duplicates = {d["company"]: d for d in result["duplicates"]}
    assert duplicates["Acme"]["status"] == "applied"
    assert duplicates["Acme"]["url"] == "https://www.linkedin.com/jobs/view/555/"
    assert duplicates["Acme"]["employer_url"] == ACME_12
    assert duplicates["Globex, Inc."]["job_id"] == shortlisted["job_id"]
    assert duplicates["Globex, Inc."]["employer_url"] == GLOBEX_A1
    # The shortlisted LinkedIn job carries both links; it has no board to check liveness.
    [job] = listed(result)
    assert job["job_id"] == shortlisted["job_id"]
    assert job["url"] == "https://www.linkedin.com/jobs/view/777/"
    assert job["employer_url"] == GLOBEX_A1
    assert "live" not in job and "new_this_run" not in job
    assert "duplicates" in result["next_step"]
    # Reported, not merged: history still has exactly the three records.
    assert len(service.store.history(100)) == 3
    assert_compact(result)

    # The next run knows both postings by their recorded employer links: not reported again.
    again = service.daily_run()
    assert again["duplicates"] == [] and again["coverage"]["duplicates"] == 0
    assert again["coverage"]["skipped_known"] == 3
    [job] = listed(again)
    assert job["apply_url"] == GLOBEX_A1  # stored on the record by the first run
    assert len(service.store.history(100)) == 3


def test_duplicate_match_needs_another_source_and_a_nearby_date():
    def record(job_id, source, title="Engineering Manager", days=1):
        job = {"title": title, "company": "Acme Inc", "source": source, "posted_at": ago(days)}
        return {"job_id": job_id, "job": job, "first_seen": ago(0)}

    posting = {
        "title": "Sr. Engineering Manager",
        "company": "Acme",
        "source": "greenhouse",
        "posted_at": ago(1),
    }
    assert _duplicate_of(posting, [record("a", "greenhouse")]) is None  # a second opening
    assert _duplicate_of(posting, [record("b", "linkedin", days=60)]) is None  # too far apart
    assert _duplicate_of(posting, [record("c", "linkedin", "Engineering Manager, Data")]) is None
    match = _duplicate_of(
        posting,
        [
            record("d", "linkedin", "Senior Engineering Manager", days=30),
            record("e", None, "Engineering Manager, Senior", days=3),
        ],
    )
    assert match["job_id"] == "e"  # the most recent of several candidates


def test_evaluation_cap_takes_newest_first_and_defers_the_rest(service):
    service.record_consent(True)
    first = service.daily_run(max_new_evaluations=2)
    assert service.provider.calls == 2
    coverage = first["coverage"]
    assert coverage["evaluated"] == 2 and coverage["deferred"] == 1
    assert coverage["input_tokens"] == 20  # FakeProvider reports 10 per call
    assert {j["title"] for j in listed(first)} == {
        "Engineering Manager",
        "Senior Engineering Manager, Platform",
    }
    assert all(j["new_this_run"] for j in listed(first))
    assert "exceeded the cap" in first["next_step"]

    second = service.daily_run(max_new_evaluations=2)
    assert service.provider.calls == 3  # only the deferred posting
    assert second["coverage"]["already_evaluated"] == 2
    assert second["coverage"]["evaluated"] == 1 and second["coverage"]["deferred"] == 0
    assert [j["title"] for j in listed(second) if j.get("new_this_run")] == []  # it's a skip

    # A changed profile invalidates earlier evaluations; they are re-evaluated under the cap.
    profile = service.store.profile() | {"preferences": ["Small teams"]}
    service.save_profile(profile)
    third = service.daily_run(max_new_evaluations=2)
    assert service.provider.calls == 5
    assert third["coverage"]["evaluated"] == 2 and third["coverage"]["deferred"] == 1
    # Running never marks anything surfaced.
    assert set(statuses(service).values()) == {"new"}


def test_ranks_new_and_carried_over_matches_under_current_strictness(service):
    service.record_consent(True)
    carried = service.evaluate(linkedin_job("Director of Engineering", "Initech", 901))
    shown = service.evaluate(linkedin_job("VP of Engineering", "Hooli", 902))
    service.status(shown["job_id"], "surfaced")

    result = service.daily_run(top_n=2)
    assert [j["title"] for j in result["new"]] == [
        "Senior Engineering Manager, Platform",  # fit 89.4, new today
        "Director of Engineering",  # fit 78.8, from an earlier run, never shown
    ]
    assert result["new"][1]["job_id"] == carried["job_id"]
    assert "new_this_run" not in result["new"][1]
    # Shown before with no decision: still open, not hidden.
    assert [j["job_id"] for j in result["open"]] == [shown["job_id"]]
    assert result["coverage"]["new_matches"] == 3  # Engineering Manager falls past top_n
    assert result["coverage"]["open_matches"] == 1
    assert result["shown_ids"] == [j["job_id"] for j in listed(result)]
    assert result["coverage"]["evaluated"] == 3  # the skipped Globex role was evaluated too
    assert result["coverage"]["strictness"] == "balanced"
    for job in listed(result):
        assert job["recommendation"] != "skip" and job["fit_percent"] is not None
    assert_compact(result)

    calls = service.provider.calls
    service.set_strictness("strict")
    strict = service.daily_run()
    assert service.provider.calls == calls  # re-scored locally, no new calls
    assert strict["coverage"]["strictness"] == "strict"
    assert [j["title"] for j in strict["new"]] == [
        "Senior Engineering Manager, Platform",
        "Director of Engineering",
    ]


def test_liveness_flags_postings_no_longer_on_their_board(service, ats):
    service.record_consent(True)
    service.evaluate(linkedin_job("Director of Engineering", "Initech", 901))
    service.daily_run()

    payload = greenhouse_payload()
    payload["jobs"] = [j for j in payload["jobs"] if j["id"] != 11]
    ats.routes[GH.format("acme") + "?content=true"] = payload
    result = service.daily_run(refresh=True)
    assert result["gone"] == [
        {
            "job_id": result["gone"][0]["job_id"],
            "company": "Acme",
            "title": "Senior Engineering Manager, Platform",
            "url": "https://job-boards.greenhouse.io/acme/jobs/11",
        }
    ]
    assert result["coverage"]["gone"] == 1 and "closed" in result["next_step"]
    assert {j["title"]: j.get("live") for j in listed(result)} == {
        "Director of Engineering": None,  # LinkedIn: no board to check
        "Engineering Manager": True,
    }
    # Closure is a fact about the posting, recorded without the agent.
    assert statuses(service)["Senior Engineering Manager, Platform"] == "closed"
    assert set(statuses(service).values()) == {"new", "closed"}

    # When the board cannot be read, absence proves nothing.
    del ats.routes[GH.format("acme") + "?content=true"]
    unread = service.daily_run(refresh=True)
    assert unread["gone"] == [] and unread["coverage"]["boards_failed"] == 1
    assert unread["coverage"]["board_errors"][0]["company"] == "Acme"
    assert all("live" not in j for j in listed(unread))


def test_rejects_out_of_range_arguments(service):
    with pytest.raises(ValueError, match="max_new_evaluations"):
        service.daily_run(max_new_evaluations=101)
    with pytest.raises(ValueError, match="top_n"):
        service.daily_run(top_n=0)
    with pytest.raises(ValueError, match="since_days"):
        service.daily_run(since_days=0)


def test_cli_daily_without_profile_or_boards(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "job_matcher.cli", "--data-dir", str(tmp_path), "daily"],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stdout
    output = json.loads(result.stdout)
    assert output["status"] == "onboarding_required"
    assert listed(output) == [] and output["unevaluated"] == []
    assert "add_job_board" in output["next_step"] and "get_onboarding" in output["next_step"]


def test_employer_link_is_saved_on_the_existing_record(service):
    service.record_consent(True)
    linked = service.evaluate(linkedin_job("Software Engineering Manager", "Globex, Inc.", 777))
    service.daily_run(max_new_evaluations=0)
    row = next(r for r in service.store.history(100) if r["job_id"] == linked["job_id"])
    assert row["job"]["apply_url"] == GLOBEX_A1
    assert row["job"]["url"] == "https://www.linkedin.com/jobs/view/777/"  # unchanged


def test_employer_links_filled_from_boards_already_read(service, ats):
    service.record_consent(True)
    # Acme 13 is 40 days old: still in the board listing, but not rediscovered this run.
    single = service.evaluate(linkedin_job("Director of Engineering", "Acme", 881))
    # Two same-title Initech postings, both too old to rediscover: ambiguous.
    payload = greenhouse_payload("Initech")
    payload["jobs"] = [
        {
            **j,
            "id": 20 + i,
            "absolute_url": f"https://job-boards.greenhouse.io/initech/jobs/2{i}",
            "first_published": ago(40),
            "updated_at": ago(40),
        }
        for i, j in enumerate([payload["jobs"][1]] * 2)
    ]
    ats.routes[GH.format("initech")] = payload
    ats.routes[GH.format("initech") + "?content=true"] = payload
    service.add_job_board("Initech", "https://boards.greenhouse.io/initech")
    ambiguous = service.evaluate(linkedin_job("Engineering Manager", "Initech", 882))
    lonely = service.evaluate(linkedin_job("Director of Engineering", "Umbrella", 883))

    result = service.daily_run(max_new_evaluations=0)
    by_id = {j["job_id"]: j for j in listed(result)}
    assert (
        by_id[single["job_id"]]["employer_url"] == "https://job-boards.greenhouse.io/acme/jobs/13"
    )
    row = next(r for r in service.store.history(100) if r["job_id"] == single["job_id"])
    assert row["job"]["apply_url"] == "https://job-boards.greenhouse.io/acme/jobs/13"
    assert by_id[ambiguous["job_id"]].get("employer_url") is None
    assert by_id[lonely["job_id"]].get("employer_url") is None
    rows = {r["job_id"]: r["job"] for r in service.store.history(100)}
    assert rows[ambiguous["job_id"]]["apply_url"] is None
    assert rows[lonely["job_id"]]["apply_url"] is None
    assert rows[ambiguous["job_id"]]["url"] == "https://www.linkedin.com/jobs/view/882/"


def test_stops_evaluating_after_repeated_provider_failures(service, monkeypatch):
    from job_matcher import daily

    monkeypatch.setattr(daily, "MAX_CONSECUTIVE_FAILURES", 2)
    service.record_consent(True)

    def unavailable(profile, job):
        service.provider.calls += 1
        raise RuntimeError("TypeSafe request failed")

    service.provider.evaluate = unavailable
    result = service.daily_run()
    assert service.provider.calls == 2  # not all three discovered postings
    coverage = result["coverage"]
    assert coverage["stopped_after_failures"] and coverage["evaluated"] == 0
    assert len(coverage["evaluation_errors"]) == 2 and coverage["deferred"] == 1
    assert "provider errors in a row" in result["next_step"]


def test_report_markdown_has_new_and_still_open_tables(service):
    service.record_consent(True)
    linked = service.evaluate(linkedin_job("Software Engineering Manager", "Globex, Inc.", 777))
    result = service.daily_run()
    report = result["report_markdown"]
    assert report.startswith("**New since your last run**")
    assert "**Still open**" in report and "Nothing waiting on a decision." in report
    rows = [line for line in report.splitlines() if line.startswith("| ") and "---" not in line]
    assert len(rows) - 1 == len(listed(result))  # one header + one row per job
    globex = next(line for line in rows if "Globex" in line)
    assert "[LinkedIn](https://www.linkedin.com/jobs/view/777/)" in globex
    assert f"[Employer]({GLOBEX_A1})" in globex
    assert "boards searched" in report and "not hiring odds" in report
    assert "report_markdown verbatim" in result["next_step"]
    assert linked["job_id"] in result["shown_ids"]


def test_report_markdown_says_when_nothing_is_new():
    from job_matcher.daily import report_markdown

    coverage = {"boards_read": 2, "discovered": 4, "evaluated": 0, "strictness": "strict"}
    report = report_markdown([], [], coverage)
    assert "Nothing new: 4 recent postings checked" in report
    assert "Nothing waiting on a decision." in report


def test_shown_jobs_move_to_still_open_and_decisions_remove_them(service):
    service.record_consent(True)
    first = service.daily_run()
    ids = first["shown_ids"]
    assert ids and all(j["status"] == "new" for j in first["new"])
    result = service.statuses(ids + ["missing"], "shown")
    assert result["updated"] == ids and result["unknown"] == ["missing"]

    second = service.daily_run()
    assert second["new"] == []  # nothing new: not padded with older matches
    assert {j["job_id"] for j in second["open"]} == set(ids)  # shown, not hidden
    assert all(j["status"] == "shown" and j["times_shown"] == 1 for j in second["open"])
    assert "Nothing new" in second["report_markdown"]

    service.status(ids[0], "applied")
    service.status(ids[1], "dismissed", reason="Not my domain")
    third = service.daily_run()
    assert {j["job_id"] for j in third["open"]} == set(ids[2:])
    assert third["coverage"]["excluded"]["decided"] == 2


def test_postings_that_return_are_reopened(service, ats):
    service.record_consent(True)
    service.daily_run()
    original = ats.routes[GH.format("acme") + "?content=true"]
    trimmed = greenhouse_payload()
    trimmed["jobs"] = [j for j in trimmed["jobs"] if j["id"] != 11]
    ats.routes[GH.format("acme") + "?content=true"] = trimmed
    assert service.daily_run(refresh=True)["coverage"]["gone"] == 1
    ats.routes[GH.format("acme") + "?content=true"] = original
    back = service.daily_run(refresh=True)
    assert [j["title"] for j in back["reopened"]] == ["Senior Engineering Manager, Platform"]
    assert statuses(service)["Senior Engineering Manager, Platform"] == "new"


def test_still_open_ages_out_but_stays_reachable(service):
    from contextlib import closing

    service.record_consent(True)
    ids = service.daily_run()["shown_ids"]
    service.statuses(ids, "shown")
    with closing(service.store.connect()) as db, db:
        db.execute(
            "UPDATE shown SET shown_at='2026-01-01T00:00:00+00:00' WHERE job_id=?", (ids[0],)
        )
    view = service.matches("open")
    assert ids[0] not in {j["job_id"] for j in view["open"]}
    assert view["excluded"]["stale"] == 1 and "include_stale" in view["note"]
    assert ids[0] in {j["job_id"] for j in service.matches("open", include_stale=True)["open"]}
    service.set_strictness("balanced", {"open_window_days": 365})
    assert ids[0] in {j["job_id"] for j in service.matches("open")["open"]}


def test_board_jobs_are_not_given_their_own_url_as_employer_link(service):
    from job_matcher.daily import report_markdown

    service.record_consent(True)
    result = service.daily_run()
    assert all("employer_url" not in j for j in listed(result))  # all came from boards
    rows = {r["job_id"]: r for r in service.store.history(100)}
    assert all(not rows[j["job_id"]]["job"].get("apply_url") for j in listed(result))
    same = {"company": "A", "title": "T", "url": "https://x/1", "apply_url": "https://x/1"}
    coverage = {"boards_read": 1, "discovered": 1, "evaluated": 0, "strictness": "balanced"}
    assert "Employer" not in report_markdown([same], [], coverage)


def test_report_shows_the_posted_salary_range():
    from job_matcher.daily import _salary, report_markdown

    one = {"description": "Base pay: $129,000.00 - $210,000.00."}
    assert _salary(one) == "$129k–$210k"
    zones = {"description": "NY $195,000 - $312,000; elsewhere $152,000 - $243,000"}
    assert _salary(zones) == "$152k–$312k (2 ranges)"
    board = {"compensation": "USD 200,000–250,000 per year", "description": "$1 - $2"}
    assert _salary(board) == "$200k–$250k"
    assert _salary({"description": "Competitive pay"}) is None

    job = {"company": "A", "title": "T", "url": "https://x/1", "salary": "$129k–$210k"}
    coverage = {"boards_read": 1, "discovered": 1, "evaluated": 0, "strictness": "balanced"}
    header, rule, row = report_markdown([job], [], coverage).splitlines()[2:5]
    assert header == "| Role | Fit | Salary | Call | Links | Notes |"
    assert rule.startswith("|---|")  # no number column padding the left edge
    assert "| $129k–$210k |" in row
    assert "| — |" in report_markdown([job | {"salary": None}], [], coverage)
