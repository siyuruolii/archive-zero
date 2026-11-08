"""迭代真实分片时序、关闭清理与严格最终校验，不用打字动画替代流。"""

import asyncio
import json

import httpx
import pytest
from conftest import ScriptedProvider

from reading_agent.agents.harness import Harness
from reading_agent.agents.loop import Event, RunReport
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider, ModelRequest, ProviderEvent, Turn
from reading_agent.agents.streaming import AnswerDecoder, StreamBufferError, event_stream
from reading_agent.services.discussion import Running


@pytest.mark.parametrize("field", ["answer", "deep_summary"])
@pytest.mark.parametrize("answer", ['中文😀：print("hello")\n换行\\路径，字段answer不是指令🚀', ""])
def test_answer_decoder_handles_every_character_boundary(field, answer):
    raw = json.dumps(
        {
            "status": "succeeded",
            "reason": '忽略字符串内的"answer"',
            "nested": {field: "不要提取"},
            "other": "错误字段",
            field: answer,
        },
        ensure_ascii=True,
    )
    decoder = AnswerDecoder(field)
    assert "".join(decoder.feed(char) for char in raw) == answer


async def test_harness_yields_answer_before_provider_finishes_and_commit(corpus):
    release, closed = asyncio.Event(), asyncio.Event()
    committed = []

    class Provider:
        async def stream(self, request):
            try:
                first = '{"status":"succeeded","reason":"完成","answer":"第一段'
                yield ProviderEvent("text", text=first)
                await release.wait()
                last = '，第二段。"}'
                yield ProviderEvent("text", text=last)
                yield ProviderEvent("turn", turn=Turn(content=first + last))
            finally:
                closed.set()

    stream = Harness(Provider(), corpus).stream(
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题"),
        commit=lambda r: committed.append(r.result),
    )
    events = []
    async with asyncio.timeout(3):
        async for event in stream:
            events.append(event)
            if event.kind == "answer_delta":
                assert not release.is_set() and not committed
                assert event.data["text"] == "第一段" and event.data["provisional"]
                break
        release.set()
        events.extend([e async for e in stream])
    assert closed.is_set() and committed[0]["answer"] == "第一段，第二段。"
    assert events[-1].kind == "result" and events[-2].kind == "harness_ended"


async def test_closing_iterator_cancels_provider_and_never_commits(corpus):
    closed = asyncio.Event()
    committed = []

    class Provider:
        async def stream(self, request):
            try:
                yield ProviderEvent("text", text='{"answer":"预览')
                await asyncio.Event().wait()
            finally:
                closed.set()

    signal = asyncio.Event()
    stream = Harness(Provider(), corpus).stream(
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题"),
        cancel=signal,
        commit=committed.append,
    )
    async with asyncio.timeout(3):
        while (await anext(stream)).kind != "answer_delta":
            pass
        await stream.aclose()
    assert signal.is_set() and closed.is_set() and not committed


async def test_preview_does_not_turn_invalid_json_into_success(corpus):
    raw = '{"status":"succeeded","reason":"完成","answer":"可见片段'
    events = [
        e
        async for e in Harness(ScriptedProvider(raw), corpus).stream(
            Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题")
        )
    ]
    assert any(e.kind == "answer_delta" for e in events)
    assert events[-1].data["report"]["end_reason"] == "invalid_final_result"


async def test_stream_queue_overflow_is_explicit_and_bounded():
    async def produce(emit):
        for i in range(4):
            emit(Event("text_delta", {"text": str(i)}))
        return RunReport()

    stream = event_stream(produce, cancel=asyncio.Event(), max_buffer=2)
    seen = []
    with pytest.raises(StreamBufferError):
        async for event in stream:
            seen.append(event)
    assert len(seen) == 2 and all(e.kind != "result" for e in seen)


def test_slow_subscriber_recovers_whole_answer_with_snapshot():
    run = Running()
    queue = asyncio.Queue(maxsize=2)
    run.subscribers.add(queue)
    for text in ("一", "二", "三"):
        run.answer_draft += text
        run.emit("answer_delta", text=text)
    event = queue.get_nowait()
    assert event["type"] == "snapshot" and event["data"]["answer_draft"] == "一二三"


async def test_provider_yields_tool_argument_fragments_before_turn():
    from test_provider import frame, settings

    payload = frame(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "x",
                    "type": "function",
                    "function": {"name": "material_read", "arguments": '{"material_id":'},
                }
            ]
        }
    )
    payload += (
        frame(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '"tools","start":0,"length":1}'}}
                ]
            },
            "tool_calls",
            {"prompt_tokens": 123},
        )
        + "data: [DONE]\n\n"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=payload
            )
        )
    ) as client:
        events = [
            e
            async for e in ChatProvider(settings(), client).stream(
                ModelRequest([{"role": "user", "content": "JSON"}], [], {}, 100)
            )
        ]
    fragments = [e.data for e in events if e.kind == "tool_call_delta"]
    assert (
        len(fragments) == 2
        and "".join(e.get("arguments", "") for e in fragments) == events[-1].turn.calls[0].arguments
    )
    assert events[-1].kind == "turn" and any(e.kind == "usage" for e in events)


async def test_deep_read_preview_before_completion_still_requires_validation(corpus):
    release = asyncio.Event()

    class Provider:
        async def stream(self, request):
            first = '{"status":"succeeded","reason":"预览","deep_summary":"精读首段'
            yield ProviderEvent("text", text=first)
            await release.wait()
            # Deliberately incomplete: no preview may bypass strict final validation.
            yield ProviderEvent("turn", turn=Turn(content=first))

    stream = Harness(Provider(), corpus).stream(
        Task(role="deep_read", material_id="tools", topic_ids=["learn"])
    )
    async with asyncio.timeout(3):
        while True:
            event = await anext(stream)
            if event.kind == "deep_summary_delta":
                assert event.data["text"] == "精读首段" and event.data["provisional"]
                assert not release.is_set()
                break
        release.set()
        events = [event async for event in stream]
    assert events[-1].data["report"]["status"] == "failed"
