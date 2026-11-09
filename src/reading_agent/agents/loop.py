"""有界模型/工具循环。只产生内存结果，不发布任何已保存事件。"""

import asyncio
import copy
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field, fields
from typing import Any, Protocol

from pydantic import Field, ValidationError

from reading_agent.agents.compact import (
    CompactContext,
    CompactError,
    CompactionService,
    input_bytes,
)
from reading_agent.agents.context import PreparedContext
from reading_agent.agents.messages import sanitize_messages
from reading_agent.agents.model_router import stage_provider
from reading_agent.agents.models import FINAL_MODELS, Task
from reading_agent.agents.provider import ModelRequest, ProviderError, ProviderEvent, Turn
from reading_agent.agents.recovery import checkpoint, fingerprint, restore
from reading_agent.agents.streaming import AnswerDecoder, event_stream
from reading_agent.agents.telemetry import context_metrics, run_metrics
from reading_agent.agents.tool import ToolSession, compact_json
from reading_agent.domain.models import StrictModel


class Provider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]: ...


class Budget(StrictModel):
    max_model_calls: int = Field(default=6, ge=1, le=100)
    max_tool_calls: int = Field(default=8, ge=0, le=100)
    max_seconds: float = Field(default=90.0, gt=0, le=600)
    # 用 UTF-8 字节上界估算 token，每字节按一 token，故意保守，不声称精确计数。
    # 历史字段名保留兼容；这是 UTF-8 字节代理上限，不是供应商精确 token 数。
    # 32 KiB 会在中文正文分段读取时过早拒绝，128 KiB 仍有界且覆盖常见多段阅读。
    context_token_budget: int = Field(default=131072, ge=100, le=200000)
    output_token_budget: int = Field(default=4096, ge=32, le=16000)
    max_response_chars: int = Field(default=32000, ge=100, le=128000)
    retry_delay: float = Field(default=0.25, ge=0, le=10)
    tool_retries: int = Field(default=2, ge=0, le=3)
    max_tool_seconds: float = Field(default=90, gt=0, le=600)


@dataclass
class Event:
    kind: str
    data: dict[str, Any]


@dataclass
class RunReport:
    role: str = ""
    material_id: str = ""
    topic_ids: list[str] = field(default_factory=list)
    model: str | None = None
    status: str = "failed"
    end_reason: str = "not_started"
    result: dict[str, Any] | None = None
    model_calls: int = 0
    tool_calls: int = 0
    elapsed_seconds: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    usage_complete: bool = True
    tools: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    readings: list[dict[str, Any]] = field(default_factory=list)
    context_usage: list[dict[str, int]] = field(default_factory=list)
    observability: dict[str, Any] = field(default_factory=dict)
    validation_errors: list[dict[str, str]] = field(default_factory=list)
    compaction_events: list[dict[str, Any]] = field(default_factory=list)
    memory_status: str = "not_requested"
    memory_error: str | None = None
    consolidation_scheduled: bool = False
    consolidation_error: str | None = None
    reading_memory_status: str = "not_requested"
    reading_memory_error: str | None = None
    # 中断时保留真实 JSON 草稿，不能把半截 JSON 当成用户可读的最终回答。
    draft: str = ""
    incomplete: bool = False
    recovery_notes: list[dict] = field(default_factory=list)


class StopRun(Exception):
    pass


class Emit(Protocol):
    def __call__(self, kind: str, **data: Any) -> None: ...


class AgentLoop:
    def __init__(
        self, provider: Provider, budget: Budget | None = None, *, compactor=None, model_router=None
    ):
        self.provider = provider
        self.budget = budget or Budget()
        self.compactor = compactor if compactor is not None else CompactionService(provider)
        self.model_router = model_router or provider

    async def stream(self, context, *, cancel=None, on_event=None, max_buffer=256):
        signal = cancel if cancel is not None else asyncio.Event()
        async with aclosing(
            event_stream(
                lambda emit: self.run(context, cancel=signal, on_event=emit),
                cancel=signal,
                observer=on_event,
                max_buffer=max_buffer,
            )
        ) as events:
            async for event in events:
                yield event

    async def run(
        self,
        context: PreparedContext,
        *,
        cancel: asyncio.Event | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> RunReport:
        task, session, messages = context.task, context.session, context.messages
        report = RunReport(
            role=task.role,
            material_id=task.material_id,
            topic_ids=list(task.topic_ids),
            model=getattr(self.provider, "model_name", None),
        )
        if context.recovery:
            data = context.recovery["report"]
            report = RunReport(
                **{f.name: data[f.name] for f in fields(RunReport) if f.name in data}
            )
            report.model = getattr(self.provider, "model_name", None)
        signal = cancel if cancel is not None else asyncio.Event()

        def emit(kind: str, **data: Any) -> None:
            tracked = kind in {
                "attempt_started",
                "model_turn_received",
                "tool_result",
                "context_compacted",
                "ended",
            }
            if tracked:
                report.observability = run_metrics(report, elapsed=time.monotonic() - started)
            if on_event:
                # ended保持Loop的最后一个事件，提交/记忆收尾仍由Harness负责。
                if kind == "ended":
                    on_event(Event("telemetry", copy.deepcopy(report.observability)))
                on_event(Event(kind, data))
                if tracked and kind != "ended":
                    on_event(Event("telemetry", copy.deepcopy(report.observability)))

        previous_elapsed = report.elapsed_seconds
        started = time.monotonic() - previous_elapsed
        self._saved = context.recovery
        self._save_checkpoint = lambda current: emit(
            "checkpoint",
            state=checkpoint(session, current, report, self.budget, time.monotonic() - started),
        )
        self._main_messages = messages
        remaining = self.budget.max_seconds - previous_elapsed
        if remaining <= 0:
            report.status = "failed"
            report.result = None
            report.end_reason = "time_budget_exhausted"
            return report
        if signal.is_set():
            report.end_reason = "cancelled"
            report.incomplete = True
            report.observability = run_metrics(report)
            return report
        work = asyncio.create_task(self._drive(task, session, messages, report, signal, emit))
        cancellation = asyncio.create_task(signal.wait())

        async def pulse():
            while True:
                await asyncio.sleep(1)
                emit("recovery_heartbeat", elapsed_seconds=round(time.monotonic() - started, 6))

        heartbeat = asyncio.create_task(pulse())
        try:
            done, _ = await asyncio.wait(
                {work, cancellation, heartbeat},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                await heartbeat  # 心跳落盘失败也停止执行，不能失去持久化后继续产生副作用。
            if signal.is_set():
                work.cancel()
                report.status = "failed"
                report.result = None
                report.end_reason = "cancelled"
                if report.model_calls:
                    report.usage_complete = False
            elif work in done:
                await work  # 程序错误/未来存储异常不吞成普通工具失败。
            else:
                work.cancel()
                report.status = "failed"
                report.result = None
                report.end_reason = "time_budget_exhausted"
                if report.model_calls:
                    report.usage_complete = False
        finally:
            for pending in (work, cancellation, heartbeat):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(work, cancellation, heartbeat, return_exceptions=True)
            report.elapsed_seconds = round(time.monotonic() - started, 6)
            report.readings = session.readable_log()
        report.incomplete = report.status != "succeeded"
        emit("ended", status=report.status, reason=report.end_reason)
        return report

    async def _drive(
        self,
        task: Task,
        session: ToolSession,
        messages: list[dict[str, Any]],
        report: RunReport,
        cancel: asyncio.Event,
        emit: Emit,
    ) -> None:
        async def summarize(schema, instruction, data):
            previous_draft = report.draft
            try:
                request = ModelRequest(
                    [
                        {
                            "role": "system",
                            "content": instruction
                            + "\nJSON Schema: "
                            + compact_json(schema.model_json_schema()),
                        },
                        {"role": "user", "content": compact_json(data)},
                    ],
                    [],
                    schema.model_json_schema(),
                    min(2048, self.budget.output_token_budget),
                )
                turn = await self._model_turn(
                    request,
                    report,
                    cancel,
                    lambda *a, **k: None,
                    allow_compaction=False,
                    purpose="session_recall",
                )
                if turn.finish_reason != "stop" or turn.calls:
                    raise ValueError("invalid session recall summary")
                return schema.model_validate_json(turn.content)
            finally:
                report.draft = previous_draft

        session.summarize = summarize
        try:
            if self._saved:
                try:
                    report.recovery_notes.extend(await restore(session, messages, self._saved))
                except ValueError as exc:
                    raise StopRun(str(exc)) from None
                if report.result is not None and report.status == "succeeded":
                    session.validate_final(FINAL_MODELS[task.role].model_validate(report.result))
                    return
            while True:
                self._check(cancel)
                request = ModelRequest(
                    messages,
                    session.schemas(),
                    FINAL_MODELS[task.role].model_json_schema(),
                    self.budget.output_token_budget,
                )
                turn = await self._model_turn(request, report, cancel, emit)
                self._check(cancel)
                if turn.finish_reason == "tool_calls":
                    if not turn.calls:
                        raise StopRun("protocol_error")
                    ids = [c.id for c in turn.calls]
                    if any(not i or len(i) > 256 for i in ids) or len(set(ids)) != len(ids):
                        raise StopRun("invalid_call_ids")
                    # 同一 call_id 跨轮次重复也拒绝，不能覆盖之前的请求/结果配对。
                    previous = {entry["call_id"] for entry in report.tools}
                    if previous.intersection(ids):
                        raise StopRun("reused_call_id")
                    messages.append(
                        {
                            "role": "assistant",
                            "content": turn.content or None,
                            "tool_calls": [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.name,
                                        "arguments": c.arguments,
                                    },
                                }
                                for c in turn.calls
                            ],
                        }
                    )
                    for call in turn.calls:
                        self._check(cancel)
                        before = time.monotonic()
                        from .tool.registry import REGISTRY

                        spec = REGISTRY.get(call.name)
                        for attempt in range(self.budget.tool_retries + 1):
                            self._check(cancel)
                            if report.tool_calls >= self.budget.max_tool_calls:
                                raise StopRun("tool_budget_exhausted")
                            report.tool_calls += 1
                            self._save_checkpoint(messages)
                            emit(
                                "tool_requested",
                                call_id=call.id,
                                name=call.name,
                                arguments=call.arguments,
                                attempt=attempt + 1,
                            )
                            try:
                                result = await asyncio.wait_for(
                                    session.aexecute(call.name, call.arguments),
                                    self.budget.max_tool_seconds,
                                )
                            except TimeoutError:
                                result = {
                                    "status": "error",
                                    "error": "tool_timeout"
                                    if spec and not spec.side_effect
                                    else "tool_execution_unknown",
                                    "data": None,
                                }
                                if spec is None or spec.side_effect:
                                    session.unknown_mutations = True
                            if result.get("error") in {
                                "tool_execution_unknown",
                                "sandbox_cleanup_unconfirmed",
                            }:
                                session.unknown_mutations = True
                            if (
                                result["status"] == "success"
                                or not spec
                                or spec.side_effect
                                or result.get("error") not in spec.retry_errors
                                or attempt >= self.budget.tool_retries
                            ):
                                break
                            emit(
                                "tool_retry_scheduled",
                                call_id=call.id,
                                name=call.name,
                                reason=result["error"],
                                attempt=attempt + 1,
                            )
                            await asyncio.sleep(self.budget.retry_delay * (2**attempt))
                        entry = {
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                            "result": result,
                            "elapsed_seconds": round(time.monotonic() - before, 6),
                        }
                        report.tools.append(entry)
                        emit("tool_result", **entry)
                        try:
                            key = fingerprint(call.name, call.arguments)
                            session.recovered_outcomes[key] = {
                                "name": call.name,
                                "arguments": call.arguments,
                                "result": result,
                                "unknown": result.get("error")
                                in {"tool_execution_unknown", "sandbox_cleanup_unconfirmed"},
                            }
                        except (ValueError, TypeError):
                            pass
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": compact_json(result),
                            }
                        )
                        self._save_checkpoint(messages)
                    continue
                if turn.finish_reason != "stop" or turn.calls:
                    raise StopRun("model_" + turn.finish_reason)
                if not turn.content.strip():
                    raise StopRun("protocol_error")
                try:
                    final = FINAL_MODELS[task.role].model_validate_json(turn.content)
                    session.validate_final(final)
                except ValidationError as exc:
                    report.validation_errors = [
                        {
                            "field": ".".join(str(x) for x in error["loc"])[:160],
                            "type": error["type"],
                        }
                        for error in exc.errors(include_input=False, include_url=False)[:10]
                    ]
                    raise StopRun("invalid_final_result") from None
                except ValueError as exc:
                    report.validation_errors = [
                        {"field": "business_constraints", "type": str(exc)[:200]}
                    ]
                    raise StopRun("invalid_final_result") from None
                report.result = final.model_dump()
                report.status = final.status
                report.end_reason = (
                    "completed" if final.status == "succeeded" else "model_reported_failure"
                )
                self._save_checkpoint(messages)
                return
        except StopRun as exc:
            report.end_reason = str(exc)
        except ProviderError as exc:
            report.end_reason = "provider_" + exc.code

    @staticmethod
    def _check(cancel: asyncio.Event) -> None:
        if cancel.is_set():
            raise StopRun("cancelled")

    async def _compact(self, request, report, cancel, emit, *, force, retrying=False):
        self._check(cancel)

        async def summarize(compact_request):
            previous_draft = report.draft
            try:
                # 压缩调用不再次压缩自身；仍共享整个任务的调用/输出/时间预算。
                return await self._model_turn(
                    compact_request,
                    report,
                    cancel,
                    lambda *args, **kwargs: None,
                    allow_compaction=False,
                )
            finally:
                report.draft = previous_draft

        try:
            result = await self.compactor.compact(
                CompactContext(
                    request,
                    min(
                        self.budget.context_token_budget,
                        getattr(
                            getattr(self.provider, "settings", None),
                            "context_byte_budget",
                            self.budget.context_token_budget,
                        ),
                    ),
                    retrying=retrying,
                ),
                force=force,
                summarize=summarize,
            )
        except CompactError as exc:
            raise StopRun("compaction_failed:" + str(exc)) from None
        except ProviderError as exc:
            raise StopRun("compaction_provider_" + exc.code) from None
        self._check(cancel)
        event = {
            "force": force,
            "changed": result.changed,
            "reason": result.reason,
            "before_bytes": result.before_bytes,
            "after_bytes": result.after_bytes,
            "covered_indices": list(result.covered_indices),
        }
        report.compaction_events.append(event)
        if result.changed:
            request.messages[:] = result.messages
            emit("context_compacted", **event)
        return result.changed

    async def _model_turn(
        self,
        request: ModelRequest,
        report: RunReport,
        cancel: asyncio.Event,
        emit: Emit,
        *,
        allow_compaction=True,
        purpose=None,
    ) -> Turn:
        failures = 0
        forced_recovery = False
        purpose = purpose or ("agent" if allow_compaction else "compaction")
        provider = (
            self.provider
            if allow_compaction
            else stage_provider(
                self.model_router, "compact" if purpose == "compaction" else purpose
            )
        )
        settings = getattr(provider, "settings", None)
        limit = min(
            self.budget.context_token_budget,
            getattr(settings, "context_byte_budget", self.budget.context_token_budget),
        )
        request.max_output_tokens = min(
            request.max_output_tokens,
            getattr(settings, "output_token_budget", request.max_output_tokens),
        )
        while True:
            self._check(cancel)
            cleaned, removed = sanitize_messages(request.messages)
            request.messages[:] = cleaned
            if removed:
                emit("messages_sanitized", removed=removed)
            if report.model_calls >= self.budget.max_model_calls:
                raise StopRun("model_budget_exhausted")
            if allow_compaction:
                await self._compact(
                    request, report, cancel, emit, force=False, retrying=failures > 0
                )
                cleaned, removed = sanitize_messages(request.messages)
                request.messages[:] = cleaned
                if removed:
                    emit("messages_sanitized", removed=removed)
            cost = input_bytes(request)
            if allow_compaction:
                report.observability["context"] = context_metrics(
                    request,
                    limit,
                    model=report.model,
                    window=getattr(settings, "context_window_tokens", None),
                )
            report.context_usage.append(
                {
                    "input_bytes": cost,
                    "output_reserve": request.max_output_tokens,
                    "limit": limit,
                }
            )
            if cost + request.max_output_tokens > limit:
                raise StopRun("context_budget_exhausted")
            # 压缩辅助调用可能已耗尽共享额度，发起本轮前再检查。
            self._check(cancel)
            if report.model_calls >= self.budget.max_model_calls:
                raise StopRun("model_budget_exhausted")
            report.model_calls += 1
            previous_usage_complete = report.usage_complete
            report.usage_complete = False
            report.draft = ""
            received: Turn | None = None
            before = time.monotonic()
            attempt_record = {
                "call": report.model_calls,
                "attempt": failures + 1,
                "purpose": purpose,
                "model": getattr(provider, "model_name", None),
                "cache_mode": getattr(settings, "cache_mode", "unknown"),
                "cache_status": "unknown",
                "cache_hit_tokens": None,
                "cache_miss_tokens": None,
                "status": "interrupted",
                "elapsed_seconds": 0.0,
            }
            report.attempts.append(attempt_record)
            self._save_checkpoint(self._main_messages)
            emit("attempt_started", call=report.model_calls, attempt=failures + 1)
            answer_decoder = (
                AnswerDecoder("deep_summary" if report.role == "deep_read" else "answer")
                if allow_compaction and report.role in {"discuss", "deep_read"}
                else None
            )
            try:
                stream = provider.stream(copy.deepcopy(request))
                try:
                    async for event in stream:
                        self._check(cancel)
                        if received is not None:
                            raise StopRun("protocol_error")
                        if event.kind == "text":
                            report.draft += event.text
                            if len(report.draft) > self.budget.max_response_chars:
                                raise StopRun("output_budget_exhausted")
                            emit("text_delta", text=event.text, call=report.model_calls)
                            if answer_decoder is not None:
                                preview = answer_decoder.feed(event.text)
                                if preview:
                                    emit(
                                        "deep_summary_delta"
                                        if report.role == "deep_read"
                                        else "answer_delta",
                                        text=preview,
                                        call=report.model_calls,
                                        provisional=True,
                                    )
                        elif event.kind == "tool_call_delta":
                            emit("tool_call_delta", **event.data, call=report.model_calls)
                        elif event.kind == "usage":
                            emit("usage_updated", usage=event.data, call=report.model_calls)
                        elif event.kind == "turn" and event.turn is not None:
                            received = event.turn
                        else:
                            raise StopRun("protocol_error")
                        await asyncio.sleep(0)  # 让迭代消费方及时接收事件，不人为延迟输出。
                finally:
                    await stream.aclose()
                if received is None:
                    raise StopRun("protocol_error")
                size = len(received.content) + sum(
                    len(c.arguments) + len(c.name) + len(c.id) for c in received.calls
                )
                if size > self.budget.max_response_chars:
                    raise StopRun("output_budget_exhausted")
                report.draft = received.content
                for key, value in received.usage.items():
                    report.usage[key] = report.usage.get(key, 0) + value
                report.usage_complete = previous_usage_complete and bool(received.usage)
                attempt_record["status"] = "received"
                attempt_record["elapsed_seconds"] = round(time.monotonic() - before, 6)
                hit = received.usage.get("prompt_cache_hit_tokens")
                miss = received.usage.get("prompt_cache_miss_tokens")
                if miss is None and hit is not None and "prompt_tokens" in received.usage:
                    miss = max(0, received.usage["prompt_tokens"] - hit)
                attempt_record["usage"] = dict(received.usage)
                attempt_record["cache_hit_tokens"] = hit
                attempt_record["cache_miss_tokens"] = miss
                attempt_record["cache_status"] = (
                    "reported" if hit is not None or miss is not None else "unknown"
                )
                if allow_compaction:
                    report.observability["context"]["input_tokens"] = received.usage.get(
                        "prompt_tokens"
                    )
                emit(
                    "model_turn_received",
                    call=report.model_calls,
                    finish_reason=received.finish_reason,
                    content=received.content,
                    calls=[
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in received.calls
                    ],
                    usage=received.usage,
                )
                return received
            except ProviderError as exc:
                attempt_record["status"] = exc.code
                attempt_record["elapsed_seconds"] = round(time.monotonic() - before, 6)
                report.usage_complete = False
                if exc.code == "context_too_long" and allow_compaction and not forced_recovery:
                    forced_recovery = True
                    emit("attempt_discarded", call=report.model_calls, reason=exc.code)
                    if not await self._compact(request, report, cancel, emit, force=True):
                        raise StopRun("context_compaction_unavailable") from None
                    continue
                if exc.code == "context_too_long":
                    raise
                if not exc.retryable or failures >= 2:
                    raise
                emit("attempt_discarded", call=report.model_calls, reason=exc.code)
                await asyncio.sleep(self.budget.retry_delay * (2**failures))
                failures += 1
            finally:
                if attempt_record["status"] in {"interrupted", "received"}:
                    attempt_record["elapsed_seconds"] = round(time.monotonic() - before, 6)
