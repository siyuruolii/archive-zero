import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import RouteFinal, Task
from reading_agent.agents.provider import (
    STREAM_BYTE_LIMIT,
    ChatProvider,
    ModelRequest,
    ProviderError,
    Settings,
)


def settings(**kwargs):
    return Settings(
        api_key="test-only-secret",
        model="test-model",
        base_url="https://model.example/v1",
        **kwargs,
    )


def frame(delta=None, finish=None, usage=None):
    chunk = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        chunk["usage"] = usage
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\r\n\r\n"


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, text):
        self.data = text.encode()
        self.closed = False

    async def __aiter__(self):
        # 刻意将 UTF-8、JSON 和 data frame 拆在不同网络块。
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]

    async def aclose(self):
        self.closed = True


def request():
    return ModelRequest(
        [{"role": "user", "content": "JSON"}], [], RouteFinal.model_json_schema(), 100
    )


async def collect(client, config=None):
    return [event async for event in ChatProvider(config or settings(), client).stream(request())]


async def test_fragmented_utf8_stream_and_tool_arguments():
    text = frame({"content": "中文\u2028仍在同一段"})
    text += frame(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "material_read", "arguments": '{"material_id":'},
                }
            ]
        }
    )
    text += frame({"tool_calls": [{"index": 0, "function": {"arguments": '"tools"}'}}]})
    text += frame(finish="tool_calls", usage={"total_tokens": 12}) + "data: [DONE]\r\n\r\n"
    stream = ByteStream(text)
    captured = []

    def handle(req):
        captured.append(req)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = await collect(client)
    turn = events[-1].turn
    assert turn.content == "中文\u2028仍在同一段"
    assert turn.calls[0].arguments == '{"material_id":"tools"}'
    assert turn.usage == {"total_tokens": 12}
    assert stream.closed
    payload = json.loads(captured[0].content)
    assert payload["stream"] is True and payload["max_tokens"] == 100
    assert captured[0].url == "https://model.example/v1/chat/completions"
    assert "test-only-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(401, False), (429, True), (503, True), (302, False)],
)
async def test_http_error_classification_and_secret_redaction(status, retryable):
    def handle(req):
        return httpx.Response(status, text="remote echo: test-only-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as caught:
            await collect(client)
    assert caught.value.code == f"http_{status}" and caught.value.retryable == retryable
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "data: {bad json}\n\n",
        'data: {"error":{"message":"secret"}}\n\n',
        "data: [DONE]\n\n",  # 没有 finish_reason
        frame({"content": "{}"}),  # 没有 DONE
        frame({"content": "{}"}, "stop") + "data: [DONE]",  # 未完成 frame
        frame({"refusal": "拒绝"}) + "data: [DONE]\n\n",
    ],
)
async def test_bad_or_truncated_stream_never_completes(text):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=text,
            )
        )
    ) as client:
        with pytest.raises(ProviderError):
            await collect(client)


async def test_network_timeout_is_retryable():
    def handle(req):
        raise httpx.ReadTimeout("unsafe remote text", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as caught:
            await collect(client)
    assert caught.value.retryable and caught.value.code == "network_error"


@pytest.mark.parametrize(
    ("content_type", "text", "expected"),
    [
        ("application/json", "{}", "expected_event_stream"),
        ("text/event-stream", "x" * (STREAM_BYTE_LIMIT + 1), "stream_size_limit"),
        ("text/event-stream", "x" * 65537, "stream_frame_size_limit"),
    ],
    ids=["wrong-content-type", "oversized-stream", "oversized-frame"],
)
async def test_wrong_content_type_and_stream_size_are_bounded(content_type, text, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"content-type": content_type},
                text=text,
            )
        )
    ) as client:
        with pytest.raises(ProviderError, match=expected):
            await collect(client)


async def test_metadata_heavy_stream_can_exceed_old_wire_limit(corpus):
    final = json.dumps(
        {"status": "succeeded", "reason": "固定资料依据" * 30, "routes": []}, ensure_ascii=False
    )
    events = []
    for char in final:
        chunk = {
            "id": "provider-metadata-" + "x" * 2000,
            "choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None}],
        }
        events.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    wire = "".join(events) + frame(finish="stop") + "data: [DONE]\n\n"
    assert 262144 < len(wire.encode()) < STREAM_BYTE_LIMIT
    assert len(final) < 32000
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=wire,
            )
        )
    ) as client:
        report = await Harness(ChatProvider(settings(), client), corpus).run(
            Task(role="route", material_id="tools", topic_ids=["learn", "engineering"])
        )
    assert report.status == "succeeded", report.end_reason
    assert report.result["reason"] == "固定资料依据" * 30


async def test_adapter_and_runner_end_to_end_without_network(corpus):
    count = 0

    def handle(req):
        nonlocal count
        count += 1
        payload = json.loads(req.content)
        if count == 1:
            body = frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "material_read",
                                "arguments": '{"material_id":"tools","start":0,"length":4000}',
                            },
                        }
                    ]
                },
                "tool_calls",
            )
        else:
            assert payload["messages"][-1]["tool_call_id"] == "c1"
            assert json.loads(payload["messages"][-1]["content"])["status"] == "success"
            body = frame(
                {
                    "content": json.dumps(
                        {
                            "status": "succeeded",
                            "reason": "完成讨论",
                            "answer": "程序检查权限后执行工具。",
                        }
                    )
                },
                "stop",
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await Harness(ChatProvider(settings(), client), corpus).run(
            Task(role="discuss", material_id="tools", topic_ids=["learn"], question="工具分工？")
        )
    assert report.status == "succeeded" and report.tool_calls == 1 and count == 2


def test_settings_require_explicit_values_and_hide_key(monkeypatch):
    for key in ["AGENT_API_KEY", "AGENT_MODEL", "AGENT_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError):
        Settings.from_env()
    assert "test-only-secret" not in repr(settings())
    with pytest.raises(ValidationError):
        Settings(api_key="replace-with-your-key", model="x", base_url="https://model.example/v1")
    for url in [
        "http://remote.example/v1",
        "https://user:secret@remote.example/v1",
        "https://remote.example/v1?key=secret",
        "file:///etc/secrets",
    ]:
        with pytest.raises(ValidationError):
            Settings(api_key="test", model="x", base_url=url)


async def test_cancellation_closes_http_stream(corpus):
    class StalledStream(httpx.AsyncByteStream):
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield frame({"content": '{"answer":'}).encode()
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = StalledStream()
    signal = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
    ) as client:
        pending = asyncio.create_task(
            Harness(ChatProvider(settings(), client), corpus).run(
                Task(role="discuss", material_id="tools", topic_ids=["learn"], question="解释"),
                cancel=signal,
            )
        )
        await asyncio.wait_for(stream.started.wait(), 1)
        signal.set()
        report = await asyncio.wait_for(pending, 1)
    assert stream.closed and report.end_reason == "cancelled"
