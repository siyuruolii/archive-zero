"""OpenAI-compatible Chat Completions HTTP adapter, with no internal retries."""

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, model_validator

from reading_agent.agents.messages import sanitize_messages
from reading_agent.agents.models import StrictModel, Text

STREAM_BYTE_LIMIT = 8 * 1024 * 1024
SSE_FRAME_BYTE_LIMIT = 64 * 1024


class Settings(StrictModel):
    api_key: SecretStr
    model: Text
    base_url: str
    adapter: Literal["chat_completions"] = "chat_completions"
    json_mode: Literal["text", "json_object", "json_schema"] = "json_object"
    supports_tools: bool = True
    strict_tools: bool = True
    cache_mode: Literal["unknown", "automatic", "unsupported"] = "unknown"
    context_byte_budget: int = Field(default=131072, ge=1000, le=200000)
    context_window_tokens: int | None = Field(default=None, ge=1, le=10000000)
    output_token_budget: int = Field(default=4096, ge=32, le=16000)
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None
    token_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    request_timeout: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def usable_configuration(self):
        key = self.api_key.get_secret_value()
        if not key.strip() or "replace-with" in key or "replace-with" in self.model:
            raise ValueError("placeholder model/key; configure AGENT_* environment variables")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("base URL must be an endpoint without credentials/query/fragment")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote model endpoints require HTTPS")
        if url.port == 0:
            raise ValueError("invalid endpoint port")
        return self

    @classmethod
    def from_env(cls):
        names = {"api_key": "AGENT_API_KEY", "model": "AGENT_MODEL", "base_url": "AGENT_BASE_URL"}
        if any(not os.environ.get(name) for name in names.values()):
            raise ValueError("set AGENT_API_KEY, AGENT_MODEL and AGENT_BASE_URL before live runs")
        return cls(
            **{key: os.environ[name] for key, name in names.items()},
            json_mode=os.getenv("AGENT_JSON_MODE", "json_object"),
            token_field=os.getenv("AGENT_TOKEN_FIELD", "max_tokens"),
        )


class ProviderError(Exception):
    """Only a safe error code escapes the transport; never echo raw response bodies."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class Turn:
    content: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ProviderEvent:
    kind: Literal["text", "tool_call_delta", "usage", "turn"]
    text: str = ""
    turn: Turn | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    output_schema: dict[str, Any]
    max_output_tokens: int


class ChatProvider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.model_name = settings.model
        self.statistics = {
            "requests": 0,
            "completed": 0,
            "cache_hit_reported_calls": 0,
            "cache_miss_reported_calls": 0,
            "cache_reported_calls": 0,
            "cache_unknown_calls": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        settings = self.settings
        if request.tools and not settings.supports_tools:
            raise ProviderError("tools_unsupported")
        tools = []
        for tool in request.tools:
            tool = {**tool, "function": dict(tool["function"])}
            if not settings.strict_tools:
                tool["function"].pop("strict", None)
            tools.append(tool)
        body: dict[str, Any] = {
            "model": settings.model,
            "messages": sanitize_messages(request.messages)[0],
            **({"tools": tools} if tools else {}),
            "stream": True,
            "stream_options": {"include_usage": True},
            settings.token_field: request.max_output_tokens,
            **(
                {"response_format": {"type": settings.json_mode}}
                if settings.json_mode != "text"
                else {}
            ),
        }
        if settings.reasoning_effort is not None:
            body["reasoning_effort"] = settings.reasoning_effort
        self.statistics["requests"] += 1
        if settings.json_mode == "json_schema":
            body["response_format"]["json_schema"] = {
                "name": "reading_result",
                "strict": True,
                "schema": request.output_schema,
            }
        try:
            async with self.client.stream(
                "POST",
                settings.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
                json=body,
                timeout=settings.request_timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    status = response.status_code
                    if status in {400, 413, 422}:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk[: 65536 - len(raw)])
                            if len(raw) >= 65536:
                                break
                        if is_context_overflow(raw):
                            raise ProviderError("context_too_long")
                    raise ProviderError(
                        f"http_{status}", retryable=status in {408, 429, 500, 502, 503, 504}
                    )
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise ProviderError("expected_event_stream")
                turn = Turn(finish_reason="")
                call_parts: dict[int, dict[str, str]] = {}
                async for data in sse_data(response):
                    if data == "[DONE]":
                        if not turn.finish_reason:
                            raise ProviderError("missing_finish_reason")
                        turn.calls = [ToolCall(**call_parts[i]) for i in sorted(call_parts)]
                        hit = turn.usage.get("prompt_cache_hit_tokens")
                        miss = turn.usage.get("prompt_cache_miss_tokens")
                        if miss is None and hit is not None and "prompt_tokens" in turn.usage:
                            miss = max(0, turn.usage["prompt_tokens"] - hit)
                        self.statistics["completed"] += 1
                        self.statistics[
                            "cache_unknown_calls"
                            if hit is None and miss is None
                            else "cache_reported_calls"
                        ] += 1
                        self.statistics["cache_hit_tokens"] += hit or 0
                        self.statistics["cache_miss_tokens"] += miss or 0
                        self.statistics["cache_hit_reported_calls"] += hit is not None
                        self.statistics["cache_miss_reported_calls"] += miss is not None
                        yield ProviderEvent("turn", turn=turn)
                        return
                    try:
                        chunk = json.loads(data)
                        if isinstance(chunk, dict) and "error" in chunk:
                            if is_context_overflow(data.encode("utf-8")):
                                raise ProviderError("context_too_long")
                            raise ProviderError("stream_api_error")
                        if not isinstance(chunk, dict):
                            raise ValueError("invalid chunk")
                        usage = chunk.get("usage")
                        if isinstance(usage, dict):
                            turn.usage.update(
                                {
                                    key: value
                                    for key, value in usage.items()
                                    if key
                                    in {
                                        "prompt_tokens",
                                        "completion_tokens",
                                        "total_tokens",
                                        "prompt_cache_hit_tokens",
                                        "prompt_cache_miss_tokens",
                                    }
                                    and type(value) is int
                                    and value >= 0
                                }
                            )
                            details = usage.get("prompt_tokens_details")
                            if (
                                isinstance(details, dict)
                                and type(details.get("cached_tokens")) is int
                                and details["cached_tokens"] >= 0
                            ):
                                if "prompt_cache_hit_tokens" not in usage:
                                    turn.usage["prompt_cache_hit_tokens"] = details["cached_tokens"]
                            yield ProviderEvent("usage", data=dict(turn.usage))
                        choices = chunk.get("choices", [])
                        if not isinstance(choices, list) or len(choices) > 1:
                            raise ValueError("expected single choice")
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("index", 0) != 0:
                            raise ValueError("unexpected choice index")
                        delta = choice.get("delta", {})
                        if delta.get("refusal"):
                            raise ProviderError("model_refusal")
                        content = delta.get("content")
                        if content:
                            if not isinstance(content, str) or turn.finish_reason:
                                raise ValueError("invalid text delta")
                            turn.content += content
                            yield ProviderEvent("text", text=content)
                        for part in delta.get("tool_calls") or []:
                            index = part["index"]
                            if (
                                type(index) is not int
                                or not 0 <= index < 32
                                or turn.finish_reason
                                or part.get("type", "function") != "function"
                            ):
                                raise ValueError("invalid tool delta")
                            dest = call_parts.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            fragments = {"id": part.get("id"), **(part.get("function") or {})}
                            for key in ("id", "name", "arguments"):
                                if fragments.get(key) is not None:
                                    if not isinstance(fragments[key], str):
                                        raise ValueError("invalid tool fragment")
                                    dest[key] += fragments[key]
                            yield ProviderEvent(
                                "tool_call_delta",
                                data={
                                    "index": index,
                                    **{
                                        key: fragments[key]
                                        for key in ("id", "name", "arguments")
                                        if fragments.get(key) is not None
                                    },
                                },
                            )
                        finish = choice.get("finish_reason")
                        if finish is not None:
                            if turn.finish_reason or not isinstance(finish, str):
                                raise ValueError("duplicate or invalid finish reason")
                            turn.finish_reason = finish
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        raise ProviderError("malformed_stream") from exc
                raise ProviderError("incomplete_stream", retryable=True)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise ProviderError("network_error", retryable=True) from exc


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """按 SSE 换行解码；不用 splitlines，避免把正文 U+2028 当分隔符。"""
    buffer = b""
    lines: list[bytes] = []
    received = 0
    frame_bytes = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        # 事件重复携带供应商元数据，传输体积不能等同于生成内容体积。
        if received > STREAM_BYTE_LIMIT:
            raise ProviderError("stream_size_limit")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            frame_bytes += len(line) + 1
            if frame_bytes > SSE_FRAME_BYTE_LIMIT:
                raise ProviderError("stream_frame_size_limit")
            line = line.removesuffix(b"\r")
            if not line:
                frame_bytes = 0
            if line.startswith(b"data:"):
                lines.append(line[5:].removeprefix(b" "))
            elif not line and lines:
                try:
                    yield b"\n".join(lines).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProviderError("invalid_stream_encoding") from exc
                lines.clear()
        if frame_bytes + len(buffer) > SSE_FRAME_BYTE_LIMIT:
            raise ProviderError("stream_frame_size_limit")
    # EOF 不把未完成 data frame 当成最终结果，交给上层报告 incomplete_stream。


def is_context_overflow(raw: bytes) -> bool:
    """只识别明确上下文错误；不暴露响应正文、不把所有 400/输出上限当作超长。"""
    try:
        value = json.loads(raw)
        error = value.get("error", {})
        if not isinstance(error, dict):
            return False
        if error.get("code") in {
            "context_length_exceeded",
            "context_window_exceeded",
            "max_context_length_exceeded",
        }:
            return True
        message = str(error.get("message", "")).casefold()
        return any(
            phrase in message
            for phrase in (
                "maximum context length",
                "context length exceeded",
                "context length exceeds",
                "context window exceeded",
                "context window exceeds",
                "context is too long",
                "context too long",
            )
        )
    except (ValueError, AttributeError):
        return False
