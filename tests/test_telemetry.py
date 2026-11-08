"""数值口径、重启回放与真实讨论管线（模型替身）验证。"""

import asyncio
import json

import httpx
import pytest
from conftest import ScriptedProvider

from reading_agent.agents.compact import input_bytes
from reading_agent.agents.harness import Harness
from reading_agent.agents.loop import RunReport
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ModelRequest, ProviderEvent, Turn
from reading_agent.agents.telemetry import context_metrics, run_metrics
from reading_agent.interfaces.api import create_app
from reading_agent.services.discussion import Discussion
from reading_agent.services.sessions import Sessions


def test_context_breakdown_is_exact_utf8_bytes_and_contains_no_content():
    request = ModelRequest(
        [
            {"role": "system", "content": "秘密规则"},
            {"role": "user", "content": "秘密材料"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "x", "function": {"name": "read", "arguments": "秘密参数"}}],
            },
            {"role": "tool", "tool_call_id": "x", "content": "秘密结果"},
        ],
        [{"name": "read"}],
        {"type": "object"},
        4096,
    )
    result = context_metrics(request, 60000, model="test", window=1000000)
    assert result["input_bytes"] == input_bytes(request)
    assert sum(result["breakdown_bytes"].values()) == result["input_bytes"]
    assert all(value >= 0 for value in result["breakdown_bytes"].values())
    assert result["budget_used"] == result["input_bytes"] + 4096
    assert result["input_tokens"] is None and result["window_tokens"] == 1000000
    assert "秘密" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "pairs,expected,covered",
    [
        ([(None, None)], None, 0),
        ([(0, 100)], 0.0, 1),
        ([(0, 0)], None, 1),
        ([(90, 10), (0, 900)], 0.09, 2),
        ([(90, 10), (None, None)], 0.9, 1),
    ],
)
def test_cache_rate_is_weighted_and_missing_is_not_zero(pairs, expected, covered):
    report = RunReport(
        model_calls=len(pairs),
        attempts=[
            {
                "status": "received",
                "cache_hit_tokens": h,
                "cache_miss_tokens": m,
                "usage": {"total_tokens": 100},
            }
            for h, m in pairs
        ],
    )
    result = run_metrics(report)
    assert result["cache"]["hit_rate"] == expected
    assert result["cache"]["reported_calls"] == covered
    assert result["cache"]["attempted_calls"] == len(pairs)
    assert result["tokens"]["prompt_tokens"] is None
    assert result["tokens"]["total_tokens"] == 100 * len(pairs)


async def test_loop_streams_progress_and_retains_terminal_event_contract(corpus):
    class Provider:
        model_name = "test-model"

        async def stream(self, request):
            yield ProviderEvent(
                "turn",
                turn=Turn(
                    content='{"status":"succeeded","reason":"完成","answer":"回答"}',
                    usage={
                        "prompt_tokens": 123,
                        "completion_tokens": 10,
                        "total_tokens": 133,
                        "prompt_cache_hit_tokens": 100,
                        "prompt_cache_miss_tokens": 23,
                    },
                ),
            )

    events = []
    report = await Harness(Provider(), corpus).run(
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题"),
        on_event=events.append,
    )
    progress = [e.data for e in events if e.kind == "telemetry"]
    assert progress[0]["phase"] == "running" and progress[0]["context"]["input_tokens"] is None
    assert progress[-1]["context"]["input_tokens"] == 123
    assert report.observability["cache"]["hit_rate"] == pytest.approx(100 / 123)
    assert [e.kind for e in events][-3:] == ["telemetry", "ended", "harness_ended"]
    assert report.observability["context"]["window_tokens"] is None


async def test_discussion_metrics_saved_in_jsonl_and_survive_new_sessions_instance(state):
    store, service, sessions, _, created, _, _ = state
    service.provider = ScriptedProvider(
        Turn(
            content='{"status":"succeeded","reason":"完成","answer":"回答"}',
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
        )
    )
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    discussion.start(bid, "问题", expected_leaf=sessions.path(bid)[-1]["id"])
    await discussion.runs[bid].task
    run = discussion.runs[bid]
    assert run.metrics["cache"]["hit_rate"] == 0.8
    answer = sessions.path(bid)[-1]["data"]
    assert answer["run_id"] == run.id and answer["metrics"]["tokens"]["total_tokens"] == 120
    assert (
        "metrics"
        not in store.one("SELECT data FROM entries WHERE id=?", (sessions.path(bid)[-1]["id"],))[
            "data"
        ]
    )
    restored = Sessions(store).last_run(bid)
    assert restored["phase"] == "completed" and restored["metrics"]["cache"]["hit_rate"] == 0.8
    assert restored["metrics"]["context"] == run.metrics["context"]


async def test_running_snapshot_and_restarted_interruption_do_not_invent_usage(state):
    store, service, sessions, _, created, _, _ = state
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting:
        model_name = "waiting"

        async def stream(self, request):
            entered.set()
            await release.wait()
            yield ProviderEvent(
                "turn", turn=Turn(content='{"status":"succeeded","reason":"完成","answer":"回答"}')
            )

    service.provider = Waiting()
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    discussion.start(bid, "问题", expected_leaf=sessions.path(bid)[-1]["id"])
    await asyncio.wait_for(entered.wait(), 2)
    try:
        snapshot = discussion.runs[bid].snapshot()
        assert snapshot["metrics"]["phase"] == "running"
        assert snapshot["metrics"]["model_calls"] == 1
        assert snapshot["metrics"]["cache"]["hit_rate"] is None
        assert snapshot["metrics"]["context"]["input_tokens"] is None
        sessions.interrupt_runs()
        restored = Sessions(store).last_run(bid)
        assert restored["phase"] == "interrupted" and restored["metrics"]["phase"] == "interrupted"
    finally:
        release.set()
        await discussion.runs[bid].task


async def test_persisted_metrics_api_requires_auth_and_does_not_expose_prompts(tmp_path):
    from desktop_fixture import seed

    # seed内部的asyncio.run不能在当前pytest事件循环中调用。
    await asyncio.to_thread(seed, str(tmp_path), observability=True)
    app = create_app(directory=tmp_path, token="token", provider=ScriptedProvider())
    bid = app.state.store.one("SELECT id FROM branches")["id"]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/api/branches/" + bid)).status_code == 401
            response = await client.get(
                "/api/branches/" + bid, headers={"authorization": "Bearer token"}
            )
    value = response.json()
    assert value["run"] is None and value["last_run"]["phase"] == "completed"
    metrics = value["last_run"]["metrics"]
    assert metrics["cache"]["hit_rate"] == 0.8
    assert metrics["context"]["window_tokens"] == 1000000
    assert "人工资料" not in json.dumps(metrics, ensure_ascii=False)
