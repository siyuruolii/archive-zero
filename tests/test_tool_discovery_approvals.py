import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import ScriptedProvider

from reading_agent.agents.models import Task
from reading_agent.agents.tool import ToolSession
from reading_agent.infrastructure.workspace import Workspace
from reading_agent.interfaces.api import create_app
from reading_agent.services.approvals import CommandApprovals
from reading_agent.storage.database import Conflict, NotFound


def session(corpus, tmp_path):
    corpus.workspace = Workspace(tmp_path / "workspace")
    return ToolSession(
        corpus, Task(role="discuss", material_id="tools", topic_ids=["learn"], question="test")
    )


def discover(tools, name):
    return tools.execute("search_tool", json.dumps({"query": name, "limit": 2}))


def test_discovery_loads_only_eligible_tools_for_current_session(corpus, tmp_path):
    tools = session(corpus, tmp_path)
    assert set(tools.names) == {"material_read", "read_file", "search_tool"}
    assert tools.execute("write_file", "{}")["error"] == "tool_not_allowed"
    assert discover(tools, "write_file")["data"][0]["name"] == "write_file"
    assert "write_file" in {schema["function"]["name"] for schema in tools.schemas()}
    assert not discover(tools, "memory_read")["data"]
    assert not discover(tools, "approve")["data"]
    assert "write_file" not in session(corpus, tmp_path).names


async def test_bash_always_requires_a_fresh_human_decision(corpus, tmp_path, monkeypatch):
    tools = session(corpus, tmp_path)
    discover(tools, "bash")
    execute = AsyncMock(return_value={"status": "success", "error": None, "data": {}})
    monkeypatch.setattr("reading_agent.agents.tool.shell.execute_container", execute)
    monkeypatch.setattr(
        "reading_agent.agents.tool.shell.select_backend",
        AsyncMock(return_value=("docker", "docker")),
    )
    args = json.dumps({"command": "printf safe", "timeout_seconds": 1})
    assert (await tools.aexecute("bash", args))["error"] == "user_confirmation_required"
    approvals = CommandApprovals(lambda: None)

    async def confirm(command, seconds, environment):
        return await approvals.request("test-scope", command, seconds)

    tools.corpus.command_approval = confirm
    first = asyncio.create_task(tools.aexecute("bash", args))
    await asyncio.sleep(0)
    assert not first.done() and execute.await_count == 0
    record = approvals.list()[0]
    assert record["command"] == "printf safe"
    approvals.resolve(record["id"], False)
    assert (await first)["error"] == "user_confirmation_denied_or_expired"
    assert execute.await_count == 0
    for _ in range(2):
        call = asyncio.create_task(tools.aexecute("bash", args))
        await asyncio.sleep(0)
        record = approvals.list()[0]
        approvals.resolve(record["id"], True)
        with pytest.raises(Conflict):
            approvals.resolve(record["id"], True)
        assert (await call)["status"] == "success"
        with pytest.raises(NotFound):
            approvals.resolve(record["id"], True)
    assert execute.await_count == 2  # No cached consent for repeated identical calls.


async def test_approval_timeout_and_task_cancellation():
    approvals = CommandApprovals(lambda: None, seconds=0.01)
    assert not await approvals.request("scope", "not executed", 1)
    assert approvals.list() == []
    work = asyncio.create_task(approvals.request("scope", "cancelled command", 1))
    await asyncio.sleep(0)
    key = approvals.list()[0]["id"]
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert approvals.list() == []
    with pytest.raises(NotFound):
        approvals.resolve(key, True)


async def test_approval_api_auth_and_exact_request(tmp_path):
    app = create_app(directory=tmp_path, token="test", provider=ScriptedProvider())
    async with app.router.lifespan_context(app):
        work = asyncio.create_task(app.state.approvals.request("scope", "printf exact", 3))
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/api/approvals")).status_code == 401
            client.headers["authorization"] = "Bearer test"
            record = (await client.get("/api/approvals")).json()[0]
            assert record["command"] == "printf exact"
            assert (
                await client.post("/api/approvals/" + record["id"], json={"approved": True})
            ).status_code == 200
            assert await work
            assert (
                await client.post("/api/approvals/" + record["id"], json={"approved": True})
            ).status_code == 404
