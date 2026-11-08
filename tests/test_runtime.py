import asyncio
import json

import pytest
from conftest import ScriptedProvider, route_item, route_json

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import Material, Task
from reading_agent.agents.provider import ProviderError, ProviderEvent, ToolCall, Turn


def task(role="route", material="tools"):
    return Task(
        role=role,
        material_id=material,
        topic_ids=["learn"],
        question="解释工具分工" if role == "discuss" else "",
    )


def tool_turn(name="material_read", arguments=None, call_id="c1"):
    args = arguments or {"material_id": "tools", "start": 0, "length": 4000}
    return Turn(calls=[ToolCall(call_id, name, json.dumps(args))], finish_reason="tool_calls")


async def test_real_tool_loop_and_request_pairing(corpus):
    provider = ScriptedProvider(tool_turn(), route_json([route_item()]))
    events = []
    report = await Harness(provider, corpus).run(task(), on_event=events.append)
    assert report.status == "succeeded"
    assert report.end_reason == "completed"
    assert (report.model_calls, report.tool_calls) == (2, 1)
    assert report.role == "route" and report.topic_ids == ["learn"]
    assert len(report.attempts) == 2 and report.attempts[0]["status"] == "received"
    assert report.tools[0]["result"]["data"]["text"] == corpus.materials["tools"].body
    messages = provider.requests[1].messages
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"] == "c1"
    assert any(event.kind == "text_delta" for event in events)
    assert events[-2].kind == "ended"
    assert events[-1].kind == "harness_ended"
    assert report.usage_complete is False  # 替身未提供 usage，不伪造零费用。


async def test_short_routing_does_not_force_body(corpus):
    report = await Harness(ScriptedProvider(route_json([route_item()])), corpus).run(
        task(material="short")
    )
    assert report.status == "succeeded"
    assert report.tool_calls == 0


async def test_zero_hit_and_explicit_failure_are_distinct(corpus):
    zero = await Harness(ScriptedProvider(route_json()), corpus).run(task())
    failed = await Harness(
        ScriptedProvider(route_json(status="failed", reason="资料不足")), corpus
    ).run(task())
    assert zero.status == "succeeded" and zero.result["routes"] == []
    assert failed.status == "failed" and failed.end_reason == "model_reported_failure"


async def test_both_topics_can_have_independent_summaries(corpus):
    learn = route_item(tag_ids=["learn-tools"])
    engineering = route_item("engineering", ["eng-runtime"])
    engineering["short_summary"] = "执行器需要检查权限、预算与取消信号。"
    report = await Harness(ScriptedProvider(route_json([learn, engineering])), corpus).run(
        Task(role="route", material_id="tools", topic_ids=["learn", "engineering"])
    )
    assert report.status == "succeeded"
    assert (
        report.result["routes"][0]["short_summary"] != report.result["routes"][1]["short_summary"]
    )


async def test_deep_success_after_body_read(corpus):
    final = {
        "status": "succeeded",
        "reason": "已读取原文",
        "deep_summary": "工具由程序校验并执行。",
        "coverage": [
            {"material_id": "tools", "start": 0, "end": len(corpus.materials["tools"].body)}
        ],
        "limitations": "",
    }
    report = await Harness(ScriptedProvider(tool_turn(), json.dumps(final)), corpus).run(
        task("deep_read")
    )
    assert report.status == "succeeded"
    assert report.readings == final["coverage"]


async def test_cannot_forge_deep_read_without_tool(corpus):
    final = {
        "status": "succeeded",
        "reason": "声称已读",
        "deep_summary": "虚假成功",
        "coverage": [{"material_id": "tools", "start": 0, "end": 10}],
        "limitations": "部分",
    }
    report = await Harness(ScriptedProvider(json.dumps(final)), corpus).run(task("deep_read"))
    assert report.end_reason == "invalid_final_result"
    assert report.result is None


async def test_missing_body_tool_error_reaches_model(corpus):
    provider = ScriptedProvider(
        tool_turn(arguments={"material_id": "missing", "start": 0, "length": 30}),
        json.dumps(
            {
                "status": "failed",
                "reason": "没有正文，无法精读",
                "deep_summary": "",
                "coverage": [],
                "limitations": "无原文",
            }
        ),
    )
    report = await Harness(provider, corpus).run(task("deep_read", "missing"))
    assert report.end_reason == "model_reported_failure"
    assert json.loads(provider.requests[1].messages[-1]["content"])["error"] == "body_unavailable"


@pytest.mark.parametrize(
    "content",
    ["", "not json", '{"status":"succeeded","routes":[]}'],
)
async def test_bad_final_never_becomes_success(corpus, content):
    report = await Harness(ScriptedProvider(content), corpus).run(task())
    assert report.status == "failed"
    assert report.result is None


async def test_forbidden_tool_is_error_not_execution(corpus):
    provider = ScriptedProvider(tool_turn("run_command", {"command": "danger"}), route_json())
    report = await Harness(provider, corpus).run(task(material="injection"))
    assert report.tools[0]["result"]["error"] == "tool_not_allowed"
    assert json.loads(provider.requests[1].messages[-1]["content"])["status"] == "error"


async def test_model_and_tool_budgets_stop_infinite_loop(corpus):
    provider = ScriptedProvider(tool_turn(), tool_turn(call_id="c2"))
    report = await Harness(provider, corpus, Budget(max_model_calls=1)).run(task())
    assert report.end_reason == "model_budget_exhausted" and len(provider.requests) == 1
    provider = ScriptedProvider(tool_turn())
    report = await Harness(provider, corpus, Budget(max_tool_calls=0)).run(task())
    assert report.end_reason == "tool_budget_exhausted" and not report.tools


async def test_large_response_is_bounded(corpus):
    report = await Harness(ScriptedProvider("x" * 101), corpus, Budget(max_response_chars=100)).run(
        task()
    )
    assert report.end_reason == "output_budget_exhausted"
    assert report.usage_complete is False


async def test_retry_same_input_and_never_reexecute_completed_tool(corpus):
    provider = ScriptedProvider(
        tool_turn(), ProviderError("network_error", retryable=True), route_json()
    )
    report = await Harness(provider, corpus, Budget(retry_delay=0.0)).run(task())
    assert report.status == "succeeded"
    assert report.model_calls == 3 and report.tool_calls == 1
    assert provider.requests[1] == provider.requests[2]
    assert report.usage_complete is False


async def test_retry_exhaustion_and_fatal_errors(corpus):
    provider = ScriptedProvider(*[ProviderError("network_error", retryable=True) for _ in range(4)])
    report = await Harness(provider, corpus, Budget(retry_delay=0.0)).run(task())
    assert report.model_calls == 3
    assert report.end_reason == "provider_network_error"
    provider = ScriptedProvider(ProviderError("http_401"))
    report = await Harness(provider, corpus).run(task())
    assert report.model_calls == 1 and report.end_reason == "provider_http_401"


class BlockingProvider:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(self, request):
        try:
            yield ProviderEvent("text", text='{"answer":"真实部分')
            self.started.set()
            await asyncio.Event().wait()
        finally:
            self.closed.set()


async def test_cancel_closes_stream_and_preserves_incomplete_draft(corpus):
    provider = BlockingProvider()
    signal = asyncio.Event()
    pending = asyncio.create_task(Harness(provider, corpus).run(task("discuss"), cancel=signal))
    await asyncio.wait_for(provider.started.wait(), 1)
    signal.set()
    report = await asyncio.wait_for(pending, 1)
    assert provider.closed.is_set()
    assert report.end_reason == "cancelled" and report.incomplete
    assert report.draft == '{"answer":"真实部分'
    assert report.result is None and report.tool_calls == 0


async def test_total_deadline_closes_stalled_provider(corpus):
    provider = BlockingProvider()
    report = await Harness(provider, corpus, Budget(max_seconds=0.03)).run(task())
    assert report.end_reason == "time_budget_exhausted"
    assert provider.closed.is_set()


async def test_pre_cancel_has_no_model_call(corpus):
    provider = ScriptedProvider()
    signal = asyncio.Event()
    signal.set()
    report = await Harness(provider, corpus).run(task(), cancel=signal)
    assert report.end_reason == "cancelled" and report.model_calls == 0


async def test_external_task_cancel_propagates_and_cleans_up(corpus):
    provider = BlockingProvider()
    pending = asyncio.create_task(Harness(provider, corpus).run(task()))
    await asyncio.wait_for(provider.started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert provider.closed.is_set()


async def test_cancel_between_tools_stops_dispatch(corpus):
    provider = ScriptedProvider(
        Turn(
            calls=[
                ToolCall("c1", "material_read", '{"material_id":"tools","start":0,"length":10}'),
                ToolCall("c2", "material_read", '{"material_id":"tools","start":10,"length":10}'),
            ],
            finish_reason="tool_calls",
        )
    )
    signal = asyncio.Event()

    def receive(event):
        if event.kind == "tool_result":
            signal.set()

    report = await Harness(provider, corpus).run(task(), cancel=signal, on_event=receive)
    assert report.end_reason == "cancelled" and report.tool_calls == 1


async def test_retry_discards_draft_instead_of_concatenating(corpus):
    class RetryProvider:
        count = 0

        async def stream(self, request):
            self.count += 1
            if self.count == 1:
                yield ProviderEvent("text", text="废弃片段")
                raise ProviderError("network_error", retryable=True)
            yield ProviderEvent("turn", turn=Turn(content=route_json()))

    events = []
    report = await Harness(RetryProvider(), corpus, Budget(retry_delay=0.0)).run(
        task(), on_event=events.append
    )
    assert report.status == "succeeded" and "废弃片段" not in report.draft
    assert any(event.kind == "attempt_discarded" for event in events)


@pytest.mark.parametrize(
    "turn",
    [
        Turn(calls=[], finish_reason="tool_calls"),
        Turn(
            calls=[
                ToolCall("same", "material_read", "{}"),
                ToolCall("same", "material_read", "{}"),
            ],
            finish_reason="tool_calls",
        ),
        Turn(content=route_json(), finish_reason="length"),
        Turn(
            content=route_json(), calls=[ToolCall("c", "material_read", "{}")], finish_reason="stop"
        ),
    ],
)
async def test_malformed_turn_protocol(corpus, turn):
    report = await Harness(ScriptedProvider(turn), corpus).run(task())
    assert report.status == "failed" and report.result is None


async def test_reused_call_id_is_rejected(corpus):
    report = await Harness(ScriptedProvider(tool_turn(), tool_turn()), corpus).run(task())
    assert report.end_reason == "reused_call_id"
    assert report.tool_calls == 1


async def test_program_errors_are_not_masked_as_tool_failures(corpus):
    class BrokenProvider:
        async def stream(self, request):
            raise RuntimeError("programming/storage error")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="programming/storage"):
        await Harness(BrokenProvider(), corpus).run(task())


async def test_usage_is_accumulated(corpus):
    first = tool_turn()
    first.usage = {"total_tokens": 100}
    second = Turn(content=route_json(), usage={"total_tokens": 30})
    report = await Harness(ScriptedProvider(first, second), corpus).run(task())
    assert report.usage == {"total_tokens": 130} and report.usage_complete


async def test_unknown_tool_result_is_paired_and_not_replayed_on_model_retry(corpus, monkeypatch):
    from reading_agent.agents.tool import ToolSession

    calls = []

    async def unknown(self, name, arguments):
        calls.append(name)
        return {"status": "unknown", "data": None, "error": "effect_unconfirmed"}

    monkeypatch.setattr(ToolSession, "aexecute", unknown)
    provider = ScriptedProvider(
        tool_turn(), ProviderError("network_error", retryable=True), route_json()
    )
    report = await Harness(provider, corpus, Budget(retry_delay=0.0)).run(task())
    assert report.status == "succeeded"
    assert calls == ["material_read"] and report.tool_calls == 1
    assert report.tools[0]["result"]["status"] == "unknown"
    assert provider.requests[1] == provider.requests[2]


def long_reading_script():
    return [
        Turn(
            calls=[
                ToolCall(
                    f"read-{i}",
                    "material_read",
                    json.dumps({"material_id": "long-cn", "start": i * 4000, "length": 4000}),
                )
            ],
            finish_reason="tool_calls",
        )
        for i in range(5)
    ] + [route_json([route_item()])]


async def test_chinese_segmented_reading_fits_bounded_default(corpus):
    corpus.materials = dict(corpus.materials) | {
        "long-cn": Material(
            id="long-cn", title="长篇 Agent 材料", source_summary="短简介", body="中文正文" * 5000
        )
    }
    task = Task(role="route", material_id="long-cn", topic_ids=["learn"])
    # 较低阈值现在会调用压缩模型，不能再用只有阅读响应的替身复现旧失败。
    # 压缩触发/恢复由 test_compact.py 的专门用例覆盖。
    report = await Harness(ScriptedProvider(*long_reading_script()), corpus).run(task)
    assert report.status == "succeeded", report.end_reason
    assert report.tool_calls == 5 and report.model_calls == 6
    assert len(report.readings) == 5
    assert report.context_usage[-1]["input_bytes"] > 32768
    assert report.context_usage[-1]["input_bytes"] < 131072


async def test_context_guard_still_rejects_oversized_initial_metadata(corpus):
    corpus.materials = dict(corpus.materials) | {
        "oversized": Material(id="oversized", title="过大简介", source_summary="中文" * 30000)
    }
    provider = ScriptedProvider()
    report = await Harness(provider, corpus).run(
        Task(role="route", material_id="oversized", topic_ids=["learn"])
    )
    assert report.end_reason == "context_budget_exhausted"
    assert report.model_calls == 0 and provider.requests == []
    assert report.context_usage[-1]["input_bytes"] > report.context_usage[-1]["limit"]
