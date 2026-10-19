import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_stdio_protocol_roundtrip(tmp_path):
    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "job_matcher.server", "--data-dir", str(tmp_path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {
                    "get_onboarding",
                    "import_application_history",
                    "save_profile",
                    "save_experience_answers",
                    "evaluate_job",
                    "evaluate_jobs",
                    "get_shortlist",
                    "get_history",
                    "set_job_status",
                    "set_strictness",
                    "record_consent",
                    "revoke_consent",
                    "add_job_board",
                    "add_boards_from_history",
                    "list_job_boards",
                    "remove_job_board",
                    "import_job_boards",
                    "find_jobs",
                    "daily_run",
                }
                result = await session.call_tool("get_onboarding", {})
                assert not result.is_error
                imported = await session.call_tool(
                    "import_application_history",
                    {
                        "entries": [
                            {
                                "title": "Manager",
                                "company": "Acme",
                                "source": "linkedin",
                                "source_id": "123",
                                "tracker_stage": "interview",
                            },
                            {},
                        ]
                    },
                )
                assert not imported.is_error
                payload = json.loads(imported.content[0].text)
                assert payload["results"][0]["status"] == "applied"
                assert payload["results"][0]["tracker_stage"] == "interview"
                assert payload["errors"][0]["index"] == 1
                body = json.loads(result.content[0].text)
                assert body["ready"] is False
                result = await session.call_tool(
                    "save_profile",
                    {
                        "profile": {
                            "resume_text": "Engineering leader with ten years managing teams.",
                            "target_roles": ["Engineering Manager"],
                        }
                    },
                )
                assert not result.is_error
                result = await session.call_tool("get_onboarding", {})
                assert json.loads(result.content[0].text)["ready"] is True
                result = await session.call_tool("get_shortlist", {})
                assert not result.is_error

    anyio.run(exercise)


def test_http_initialize_and_list_tools(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_evaluator import FakeProvider

    from job_matcher.server import create_server

    monkeypatch.setattr("job_matcher.service.JevProvider", FakeProvider)
    app = create_server(tmp_path).streamable_http_app(json_response=True, stateless_http=True)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        response = client.post(
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
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "job-matcher"
        response = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 19

        tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
        assert set(tools["evaluate_job"]["inputSchema"]["required"]) == {"job"}
        assert set(tools["evaluate_jobs"]["inputSchema"]["required"]) == {"jobs"}
        assert (
            tools["import_application_history"]["inputSchema"]["properties"]["entries"]["type"]
            == "array"
        )
        assert tools["import_application_history"]["annotations"]["openWorldHint"] is False
        assert tools["daily_run"]["annotations"]["openWorldHint"] is True
        assert tools["daily_run"]["annotations"]["readOnlyHint"] is False
        assert tools["daily_run"]["inputSchema"].get("required", []) == []

        def call(name, arguments):
            response = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
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

        assert call("get_onboarding", {})["ready"] is False
        assert call(
            "save_profile",
            {
                "profile": {
                    "resume_text": "Ten years managing engineers",
                    "target_roles": ["Engineering Manager"],
                }
            },
        )["ready"]
        saved_answers = call(
            "save_experience_answers",
            {
                "answers": [
                    {
                        "topic": "ai_development",
                        "status": "reported",
                        "statement": "I use AI to draft tests and review the results.",
                        "user_confirmed": True,
                    }
                ]
            },
        )
        assert "ai_development" in saved_answers["interview"]["answered_topics"]
        assert tools["save_experience_answers"]["annotations"]["openWorldHint"] is False
        assert "answers" in tools["save_experience_answers"]["inputSchema"]["required"]
        imported = call(
            "import_application_history",
            {
                "entries": [
                    {
                        "title": "Manager",
                        "company": "Acme",
                        "source": "linkedin",
                        "source_id": "123",
                        "tracker_stage": "applied",
                    }
                ]
            },
        )
        job_id = imported["results"][0]["job_id"]
        job = {
            "title": "Manager",
            "company": "Acme",
            "description": "Lead a remote product engineering team with salary $250000.",
            "url": "https://linkedin.com/jobs/view/123/",
        }
        assert call("get_onboarding", {})["consent"]["granted"] is False
        assert call("record_consent", {"user_confirmed": True})["granted"] is True
        evaluated = call("evaluate_job", {"job": job})
        assert evaluated["job_id"] == job_id and evaluated["consent"]["version"] == "1"
        assert call("evaluate_jobs", {"jobs": [job]})["shortlist"] == []
        shortlist = call("get_shortlist", {})
        assert shortlist["new"] == [] and shortlist["open"] == []
        assert shortlist["excluded"]["historical"] == 1
        assert call("get_history", {})[0]["historical"]
        assert (
            call("set_job_status", {"job_id": job_id, "status": "interview"})["status"] == "applied"
        )
        dismissed = call(
            "set_job_status", {"job_id": job_id, "status": "dismissed", "reason": "Too far"}
        )
        assert dismissed["status"] == "dismissed"
        assert call("get_history", {"detail": True})[0]["decision_reason"] == "Too far"
        batch = call("set_job_status", {"job_ids": [job_id, "nope"], "status": "applied"})
        assert batch["updated"] == [job_id] and batch["unknown"] == ["nope"]
        daily = call("daily_run", {"max_new_evaluations": 5, "top_n": 3})
        assert daily["status"] == "complete" and daily["new"] == daily["open"] == []
        assert "add_job_board" in daily["next_step"] and "shown" in daily["next_step"]


def test_value_errors_reach_the_agent(tmp_path):
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    from job_matcher.server import create_server

    server = create_server(tmp_path)

    async def call():
        await server.call_tool(
            "set_job_status", {"job_id": "x", "status": "applied", "reason": "receipt"}
        )

    with pytest.raises(ToolError, match="reason is only stored with status dismissed") as info:
        anyio.run(call)
    assert not isinstance(info.value, UnexpectedToolError)
