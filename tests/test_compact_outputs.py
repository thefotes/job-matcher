import json

import anyio
import pytest
from test_evaluator import FakeProvider

from job_matcher.service import Service


@pytest.fixture()
def populated(tmp_path):
    service = Service(tmp_path, FakeProvider())
    service.save_profile(
        {
            "resume_text": "Ten years managing engineering teams",
            "target_roles": ["Engineering Manager"],
        }
    )
    service.record_consent(True)
    jobs = [
        {
            "title": "Engineering Manager",
            "company": "Acme",
            "description": "Manage a remote team of engineers with salary $250000.",
            "url": "https://example.com/jobs/1",
        },
        {
            "title": "Director of Engineering",
            "company": "Globex",
            "description": "Lead multiple engineering teams with salary $300000.",
            "url": "https://example.com/jobs/2",
        },
    ]
    for job in jobs:
        service.evaluate(job)
    return service


def test_compact_history_omits_description_and_answers(populated):
    full = populated.history(limit=1)
    assert "description" in json.dumps(full)
    assert "answers" in json.dumps(full)
    compact = populated.history(limit=100, detail=False)
    assert len(compact) == 2
    text = json.dumps(compact)
    assert "description" not in text
    assert "answers" not in text
    row = compact[0]
    assert {"job_id", "status", "company", "title", "recommendation", "fit_percent"} <= set(row)


def test_history_status_filter(populated):
    first = populated.history()[0]
    populated.status(first["job_id"], "applied")
    assert [r["job_id"] for r in populated.history(limit=100, status="applied")] == [
        first["job_id"]
    ]
    assert populated.history(limit=100, status="new")
    assert populated.history(limit=100, status="dismissed") == []
    # Legacy names filter by the state they now mean.
    assert populated.history(limit=100, status="interview") == populated.history(
        limit=100, status="applied"
    )


def test_compact_rows_report_showings(populated):
    first, second = populated.history()
    populated.status(first["job_id"], "shown")
    populated.status(first["job_id"], "shown")
    compact = {r["job_id"]: r for r in populated.history(detail=False)}
    shown = compact[first["job_id"]]
    assert shown["status"] == "shown" and shown["times_shown"] == 2
    assert shown["last_shown_at"]
    unseen = compact[second["job_id"]]
    assert unseen["status"] == "new"
    assert "times_shown" not in unseen and "last_shown_at" not in unseen


def test_history_query_filter(populated):
    matches = populated.history(limit=100, detail=False, query="acme")
    assert len(matches) == 1 and matches[0]["company"] == "Acme"
    matches = populated.history(limit=100, detail=False, query="DIRECTOR")
    assert len(matches) == 1 and matches[0]["title"] == "Director of Engineering"
    assert populated.history(limit=100, query="nonexistent") == []
    assert len(populated.history(limit=100, query="engineering")) == 2
    assert populated.history(limit=1, detail=False, query="acme")[0]["company"] == "Acme"


def test_compact_shortlist_omits_description_and_answers(populated):
    full = populated.shortlist()
    assert full and "description" in json.dumps(full) and "answers" in json.dumps(full)
    compact = populated.shortlist(detail=False)
    assert len(compact) == 2
    text = json.dumps(compact)
    assert "description" not in text
    assert "answers" not in text
    assert {"job_id", "status", "company", "title", "fit_percent"} <= set(compact[0])


def test_onboarding_compact_flags(tmp_path):
    resume = ("Resume " * 100).strip()
    service = Service(tmp_path, FakeProvider())
    service.save_profile({"resume_text": resume, "target_roles": ["Engineering Manager"]})
    full = service.onboarding()
    assert full["profile"]["resume_text"] == resume
    assert "schema" in full
    compact = service.onboarding(include_resume=False, include_schema=False)
    assert compact["profile"]["resume_text"] == ""
    assert compact["resume_chars"] == len(resume)
    assert "schema" not in compact
    schema_only = service.onboarding(include_resume=False)
    assert "schema" in schema_only and schema_only["profile"]["resume_text"] == ""
    resume_only = service.onboarding(include_schema=False)
    assert "schema" not in resume_only


def test_mcp_tools_compact_by_default(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from job_matcher.server import create_server

    monkeypatch.setattr("job_matcher.service.JevProvider", FakeProvider)
    app = create_server(tmp_path).streamable_http_app(json_response=True, stateless_http=True)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )

        def call(name, arguments):
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert not result.get("isError", False), result
            if name in {"get_history"}:
                return [json.loads(item["text"]) for item in result["content"]]
            return json.loads(result["content"][0]["text"])

        resume = "Ten years managing engineers"
        call(
            "save_profile",
            {"profile": {"resume_text": resume, "target_roles": ["Engineering Manager"]}},
        )
        call("record_consent", {"user_confirmed": True})
        call(
            "evaluate_job",
            {
                "job": {
                    "title": "Engineering Manager",
                    "company": "Acme",
                    "description": "Manage a remote team with salary $250000.",
                    "url": "https://example.com/jobs/1",
                }
            },
        )
        history = call("get_history", {})
        text = json.dumps(history)
        assert "description" not in text
        assert "answers" not in text
        assert call("get_history", {"detail": True})[0]["job"]["description"]
        assert call("get_history", {"status": "new", "query": "acme"})
        assert call("get_history", {"status": "applied"}) == []
        shortlist = call("get_shortlist", {})
        text = json.dumps(shortlist)
        assert "description" not in text
        assert "answers" not in text
        assert call("get_shortlist", {"detail": True})["new"][0]["job"]["description"]
        compact = call("get_onboarding", {})
        assert compact["profile"]["resume_text"] == ""
        assert compact["resume_chars"] == len(resume)
        assert "schema" not in compact
        full = call("get_onboarding", {"include_resume": True, "include_schema": True})
        assert full["profile"]["resume_text"] == resume
        assert "schema" in full


def test_stdio_onboarding_compact_by_default(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def exercise():
        import os
        import sys
        from pathlib import Path

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "job_matcher.server", "--data-dir", str(tmp_path)],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "save_profile",
                    {
                        "profile": {
                            "resume_text": "Ten years managing engineers",
                            "target_roles": ["Engineering Manager"],
                        }
                    },
                )
                assert not result.is_error
                compact = json.loads(
                    (await session.call_tool("get_onboarding", {})).content[0].text
                )
                assert compact["profile"]["resume_text"] == ""
                assert compact["resume_chars"] == len("Ten years managing engineers")
                assert "schema" not in compact
                full = json.loads(
                    (await session.call_tool("get_onboarding", {"include_resume": True}))
                    .content[0]
                    .text
                )
                assert full["profile"]["resume_text"] == "Ten years managing engineers"

    anyio.run(exercise)
