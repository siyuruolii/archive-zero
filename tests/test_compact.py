import asyncio
import json

import httpx
import pytest
from conftest import ScriptedProvider
from test_provider import settings

from reading_agent.agents.compact import (
    CompactContext,
    CompactionService,
    input_bytes,
)
from reading_agent.agents.context import build_context
from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import FINAL_MODELS, HistoryMessage, Task
from reading_agent.agents.prompts import RuntimeEnvironment
from reading_agent.agents.provider import (
    ChatProvider,
    ModelRequest,
    ProviderError,
    ProviderEvent,
    Turn,
)
from reading_agent.agents.tool import ToolSession

SUMMARY = '{"summary":"保留用户目标、UTF-8约束、来源及未完成项。","retained_constraints":["UTF-8"]}'


def history_task():
    return Task(role="route", material_id="tools", topic_ids=["learn"])


def discussion_task():
    history = []
    for index in range(4):
        history.extend(
            [
                HistoryMessage(role="user", content=f"旧问题{index}：" + "文字" * 200),
                HistoryMessage(role="assistant", content=f"旧回答{index}：" + "事实" * 200),
            ]
        )
    return Task(
        role="discuss",
        material_id="tools",
        topic_ids=["learn"],
        question="当前问题必须保留",
        history=history,
        session_root="稳定根必须保留",
    )


class SpyCompactor(CompactionService):
    def __init__(self):
        super().__init__()
        self.forces = []

    async def compact(self, context, *, force=False, summarize=None):
        self.forces.append(force)
        return await super().compact(context, force=force, summarize=summarize)


def answer():
    return '{"status":"succeeded","reason":"已回答","answer":"保留当前问题与来源。"}'


async def test_preflight_compacts_when_threshold_is_reached(corpus):
    task = discussion_task()
    runtime = RuntimeEnvironment()
    request = ModelRequest(
        build_context(corpus, task, runtime=runtime),
        ToolSession(corpus, task).schemas(),
        FINAL_MODELS[task.role].model_json_schema(),
        4096,
    )
    budget = Budget(context_token_budget=input_bytes(request) + 4096 + 100)
    provider = ScriptedProvider(SUMMARY, answer())
    report = await Harness(provider, corpus, budget, runtime=runtime).run(task)
    assert report.status == "succeeded", report.end_reason
    assert report.model_calls == 2
    assert [a["purpose"] for a in report.attempts] == ["compaction", "agent"]
    sent = provider.requests[-1].messages
    assert sent[:2] == request.messages[:2]
    assert sent[-1] == request.messages[-1]
    assert "旧问题3" in json.dumps(sent, ensure_ascii=False)
    assert "旧问题0" not in json.dumps(sent, ensure_ascii=False)
    assert report.compaction_events[0]["changed"]


async def test_context_overflow_forces_once_then_preflights_retry(corpus):
    compactor = SpyCompactor()
    provider = ScriptedProvider(ProviderError("context_too_long"), SUMMARY, answer())
    report = await Harness(provider, corpus, compactor=compactor).run(discussion_task())
    assert report.status == "succeeded", report.end_reason
    assert compactor.forces == [False, True, False]
    assert report.model_calls == 3
    assert len(provider.requests[-1].messages) < len(provider.requests[0].messages)


async def test_second_overflow_does_not_loop_or_retry_as_network_error(corpus):
    compactor = SpyCompactor()
    provider = ScriptedProvider(
        ProviderError("context_too_long"),
        SUMMARY,
        ProviderError("context_too_long", retryable=True),
    )
    report = await Harness(provider, corpus, compactor=compactor).run(discussion_task())
    assert report.end_reason == "provider_context_too_long"
    assert compactor.forces.count(True) == 1 and report.model_calls == 3


async def test_forced_without_eligible_content_stops(corpus):
    provider = ScriptedProvider(ProviderError("context_too_long"))
    report = await Harness(provider, corpus).run(history_task())
    assert report.end_reason == "context_compaction_unavailable"
    assert report.model_calls == 1


async def test_output_truncation_never_forces_compaction(corpus):
    compactor = SpyCompactor()
    report = await Harness(
        ScriptedProvider(Turn(content="{}", finish_reason="length")), corpus, compactor=compactor
    ).run(discussion_task())
    assert report.end_reason == "model_length" and compactor.forces == [False]


async def test_network_retries_preflight_but_keep_identical_input(corpus):
    compactor = SpyCompactor()
    provider = ScriptedProvider(ProviderError("network_error", retryable=True), answer())
    report = await Harness(provider, corpus, Budget(retry_delay=0.0), compactor=compactor).run(
        discussion_task()
    )
    assert report.status == "succeeded"
    assert compactor.forces == [False, False]
    assert provider.requests[0] == provider.requests[1]


async def test_compaction_api_overflow_never_recursively_compacts(corpus):
    compactor = SpyCompactor()
    provider = ScriptedProvider(
        ProviderError("context_too_long"), ProviderError("context_too_long")
    )
    report = await Harness(provider, corpus, compactor=compactor).run(discussion_task())
    assert report.end_reason == "compaction_provider_context_too_long"
    assert report.model_calls == 2 and compactor.forces == [False, True]


async def test_summary_calls_share_total_call_budget(corpus):
    provider = ScriptedProvider(ProviderError("context_too_long"), SUMMARY)
    report = await Harness(provider, corpus, Budget(max_model_calls=2)).run(discussion_task())
    assert report.end_reason == "model_budget_exhausted"
    assert report.model_calls == 2


def tool_messages(*, incomplete=False):
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "配置"},
        {"role": "user", "content": "当前任务"},
    ]
    for index in range(3):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "material_read", "arguments": "{}"},
                    }
                ],
            }
        )
        if incomplete and index == 2:
            break
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "content": json.dumps(
                    {
                        "status": "success",
                        "data": {
                            "material_id": "m",
                            "start": index * 100,
                            "end": index * 100 + 100,
                            "text": "正文" * 1500,
                        },
                    }
                ),
            }
        )
    return messages


async def test_complete_old_tool_pairs_compact_latest_pair_and_metadata_survive():
    messages = tool_messages()
    original = json.loads(json.dumps(messages))

    async def summarize(request):
        return Turn(content=SUMMARY)

    result = await CompactionService().compact(
        CompactContext(ModelRequest(messages, [], {}, 1024), 131072),
        force=True,
        summarize=summarize,
    )
    assert result.changed and result.after_bytes < result.before_bytes
    assert result.messages[-2:] == original[-2:]
    assert result.messages[:3] == original[:3]
    assert "c0" in result.messages[3]["content"] and '"start": 0' in result.messages[3]["content"]
    assert messages == original  # 失败时不能丢失原上下文。


async def test_unfinished_pair_is_never_split_or_compressed():
    messages = tool_messages(incomplete=True)

    async def forbidden(request):
        raise AssertionError("must not dispatch compression")

    result = await CompactionService().compact(
        CompactContext(ModelRequest(messages, [], {}, 1024), 131072),
        force=True,
        summarize=forbidden,
    )
    assert not result.changed and result.messages == messages


async def test_context_recovery_never_reexecutes_completed_tool(corpus):
    from test_runtime import tool_turn

    task = discussion_task()
    # 完整历史足够供强制压缩，已完成的工具执行次数必须仍然为一。
    provider = ScriptedProvider(tool_turn(), ProviderError("context_too_long"), SUMMARY, answer())
    report = await Harness(provider, corpus).run(task)
    assert report.status == "succeeded", report.end_reason
    assert report.tool_calls == 1 and report.model_calls == 4
    assert provider.requests[-1].messages[-1]["role"] == "tool"
    assert provider.requests[-1].messages[-1]["tool_call_id"] == "c1"


async def test_cancel_during_summary_closes_stream_and_preserves_agent_draft(corpus):
    class BlockingSummary:
        def __init__(self):
            self.started, self.closed = asyncio.Event(), asyncio.Event()
            self.count = 0

        async def stream(self, request):
            self.count += 1
            if self.count == 1:
                yield ProviderEvent("text", text="旧模型的真实片段")
                raise ProviderError("context_too_long")
            try:
                yield ProviderEvent("text", text="不能保存为用户回复的压缩片段")
                self.started.set()
                await asyncio.Event().wait()
            finally:
                self.closed.set()

    provider = BlockingSummary()
    signal = asyncio.Event()
    pending = asyncio.create_task(Harness(provider, corpus).run(discussion_task(), cancel=signal))
    await asyncio.wait_for(provider.started.wait(), 1)
    signal.set()
    report = await asyncio.wait_for(pending, 1)
    assert report.end_reason == "cancelled" and provider.closed.is_set()
    assert "压缩片段" not in report.draft
    assert report.result is None


async def test_invalid_summary_leaves_input_unchanged(corpus):
    compactor = SpyCompactor()
    provider = ScriptedProvider(ProviderError("context_too_long"), "not JSON")
    report = await Harness(provider, corpus, compactor=compactor).run(discussion_task())
    assert report.end_reason == "compaction_failed:invalid_compaction_result"
    assert report.result is None and report.model_calls == 2
    assert report.compaction_events[0]["changed"] is False


async def test_bigger_summary_is_rejected_without_deleting_input():
    messages = tool_messages()
    original = json.loads(json.dumps(messages))

    async def too_big(request):
        return Turn(content=json.dumps({"summary": "字" * 16000, "retained_constraints": []}))

    result = await CompactionService().compact(
        CompactContext(ModelRequest(messages, [], {}, 1024), 131072), force=True, summarize=too_big
    )
    assert not result.changed and result.reason == "not_smaller"
    assert result.messages == original


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (400, {"code": "context_length_exceeded"}, "context_too_long"),
        (400, {"message": "This model's maximum context length is 100 tokens"}, "context_too_long"),
        (413, {"code": "context_window_exceeded"}, "context_too_long"),
        (400, {"message": "max_tokens exceeds maximum output tokens"}, "http_400"),
        (400, {"message": "invalid request, echoed test-only-secret"}, "http_400"),
        (401, {"code": "context_length_exceeded"}, "http_401"),
    ],
)
async def test_provider_only_classifies_explicit_context_errors(status, error, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": error}))
    ) as client:
        with pytest.raises(ProviderError) as exc:
            async for _ in ChatProvider(settings(), client).stream(ModelRequest([], [], {}, 100)):
                pass
    assert exc.value.code == code and "secret" not in str(exc.value)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ({"code": "context_length_exceeded"}, "context_too_long"),
        ({"message": "unrelated failure"}, "stream_api_error"),
    ],
)
async def test_provider_classifies_explicit_error_inside_sse(error, code):
    content = "data: " + json.dumps({"error": error}) + "\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=content
            )
        )
    ) as client:
        with pytest.raises(ProviderError) as exc:
            async for _ in ChatProvider(settings(), client).stream(ModelRequest([], [], {}, 100)):
                pass
    assert exc.value.code == code


async def test_network_retry_cannot_change_context_after_oversized_summary(corpus):
    task = discussion_task()
    request = ModelRequest(
        build_context(corpus, task),
        ToolSession(corpus, task).schemas(),
        FINAL_MODELS[task.role].model_json_schema(),
        4096,
    )
    huge_summary = json.dumps(
        {"summary": "字" * 16000, "retained_constraints": []}, ensure_ascii=False
    )
    provider = ScriptedProvider(
        huge_summary, ProviderError("network_error", retryable=True), answer()
    )
    budget = Budget(context_token_budget=input_bytes(request) + 4096 + 100, retry_delay=0.0)
    report = await Harness(provider, corpus, budget).run(task)
    assert report.status == "succeeded", report.end_reason
    assert report.model_calls == 3  # 一次压缩、主请求失败、相同请求网络重试。
    assert provider.requests[1] == provider.requests[2]
    assert report.compaction_events[-1]["reason"] == "retry_context_frozen"


async def test_http_context_error_integrates_with_loop_force_recovery(corpus):
    from test_provider import frame

    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        content = SUMMARY if len(bodies) == 2 else answer()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=frame({"content": content}, "stop") + "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await Harness(ChatProvider(settings(), client), corpus).run(discussion_task())
    assert report.status == "succeeded", report.end_reason
    assert len(bodies) == 3 and bodies[1].get("tools", []) == []
    assert bodies[0]["messages"][:2] == bodies[2]["messages"][:2]
    assert report.compaction_events[1]["force"] and report.compaction_events[1]["changed"]


async def test_tool_only_compaction_keeps_reading_evidence_for_final_validation(corpus):
    from test_runtime import tool_turn

    final = json.dumps(
        {
            "status": "succeeded",
            "reason": "已读取正文",
            "deep_summary": "工具分工与边界。",
            "coverage": [
                {"material_id": "tools", "start": 0, "end": len(corpus.materials["tools"].body)}
            ],
            "limitations": "",
        }
    )
    provider = ScriptedProvider(
        tool_turn(call_id="c1"),
        tool_turn(call_id="c2"),
        ProviderError("context_too_long"),
        SUMMARY,
        final,
    )
    report = await Harness(provider, corpus).run(
        Task(role="deep_read", material_id="tools", topic_ids=["learn"])
    )
    assert report.status == "succeeded", report.end_reason
    assert report.tool_calls == 2 and len(report.readings) == 1
    assert report.result["coverage"] == report.readings
    assert provider.requests[-1].messages[-1]["tool_call_id"] == "c2"
