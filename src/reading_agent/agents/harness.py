"""Agent 统一入口：准备上下文、转发 Loop 事件、等待提取落盘后按需启动后台沉淀。"""

import asyncio
import copy
from collections.abc import Callable
from contextlib import aclosing
from inspect import isawaitable

from .compact import compact_entry_path
from .context import PreparedContext, build_context
from .inference import infer
from .loop import AgentLoop, Budget, Event, RunReport
from .model_router import stage_provider
from .models import Task
from .prompts import HarnessLifecycle, RuntimeEnvironment
from .streaming import event_stream
from .tool import ToolSession

__all__ = ["Budget", "Event", "Harness", "RunReport"]


class Harness:
    def __init__(
        self,
        provider,
        corpus=None,
        budget: Budget | None = None,
        *,
        memory=None,
        sessions=None,
        compactor=None,
        runtime: RuntimeEnvironment | None = None,
    ):
        self.provider = provider
        self.corpus = corpus
        self.budget = budget or Budget()
        self.memory = memory
        self.sessions = sessions
        self.compactor = compactor
        self.runtime = runtime

    def prepare(
        self, task: Task, *, branch_id=None, question_entry_id=None, result_commit=False
    ) -> PreparedContext:
        task = task.model_copy(deep=True)
        if branch_id is not None:
            if self.sessions is None or question_entry_id is None or task.role != "discuss":
                raise ValueError("discussion history requires sessions and current question entry")
            root, history = self.sessions.context(branch_id, exclude_ids={question_entry_id})
            task = Task.model_validate(
                {**task.model_dump(), "session_root": root, "history": history}
            )
        corpus = self.corpus(task) if callable(self.corpus) else self.corpus
        if corpus is None:
            raise ValueError("agent run requires a corpus")
        corpus = copy.deepcopy(corpus)
        if self.memory is not None:
            self.memory.attach(corpus, task)
            if hasattr(self.memory, "bind_sessions"):
                self.memory.bind_sessions(corpus, branch_id=branch_id)
        session = ToolSession(corpus, task)
        consolidator = getattr(self.memory, "consolidator", None)
        policy = getattr(consolidator, "policy", None)
        messages = build_context(
            corpus,
            task,
            session=session,
            runtime=self.runtime,
            lifecycle=HarnessLifecycle(
                history_loaded=branch_id is not None,
                result_commit=result_commit,
                reading_memory=hasattr(self.memory, "remember_readings"),
                user_memory_extraction=(
                    result_commit and branch_id is not None and hasattr(self.memory, "extract")
                ),
                consolidation_check=(
                    hasattr(self.memory, "maybe_consolidate") and (policy is None or policy.enabled)
                ),
            ),
        )
        if branch_id is not None and hasattr(self.sessions, "prepare_model_input"):
            messages[-1]["content"] = self.sessions.prepare_model_input(
                question_entry_id, task.question, messages[-1]["content"], branch_id=branch_id
            )
        return PreparedContext(task, session, messages)

    async def infer(self, schema, instruction, data, *, stage="history_select"):
        """业务辅助推断也经统一入口，不获得工具或记忆写权限。"""
        return await infer(stage_provider(self.provider, stage), schema, instruction, data)

    async def compact_history(self, path, *, force=True):
        return await compact_entry_path(stage_provider(self.provider, "compact"), path, force=force)

    async def stream(self, task, *, cancel=None, on_event=None, max_buffer=256, **kwargs):
        signal = cancel if cancel is not None else asyncio.Event()
        async with aclosing(
            event_stream(
                lambda emit: self.run(task, cancel=signal, on_event=emit, **kwargs),
                cancel=signal,
                observer=on_event,
                max_buffer=max_buffer,
            )
        ) as events:
            async for event in events:
                yield event

    async def run(
        self,
        task: Task,
        *,
        cancel: asyncio.Event | None = None,
        on_event: Callable[[Event], None] | None = None,
        commit=None,
        branch_id=None,
        question_entry_id=None,
        recovery=None,
    ) -> RunReport:
        if branch_id is not None and self.memory is not None and commit is None:
            raise ValueError("memory extraction requires an explicit result commit")
        context = self.prepare(
            task,
            branch_id=branch_id,
            question_entry_id=question_entry_id,
            result_commit=commit is not None,
        )
        context.recovery = recovery

        def forward(event):
            if on_event is not None:
                on_event(event)

        report = await AgentLoop(
            stage_provider(self.provider, task.role),
            self.budget,
            compactor=self.compactor,
            model_router=self.provider,
        ).run(context, cancel=cancel, on_event=forward)
        # 即使失败也允许业务保存工具事实/未完成草稿，但绝不据此提炼记忆。
        # 提交冲突/存储异常向外传播；不会跨过提交继续提炼或发布完成。
        if commit is not None:
            try:
                pending = commit(report)
                if isawaitable(pending):
                    await pending
            except Exception as exc:
                forward(Event("harness_failed", {"stage": "commit", "error": type(exc).__name__}))
                raise
        if self.memory is not None and hasattr(self.memory, "remember_readings"):
            try:
                self.memory.remember_readings(context.task, context.session.corpus, report)
                report.reading_memory_status = "completed"
            except Exception as exc:
                report.reading_memory_status = "failed"
                report.reading_memory_error = type(exc).__name__
            forward(
                Event(
                    "reading_memory_finished",
                    {"status": report.reading_memory_status, "error": report.reading_memory_error},
                )
            )
        if self.memory is not None and branch_id is not None:
            report.memory_status = "skipped"
            if report.status == "succeeded" and not (cancel is not None and cancel.is_set()):
                try:
                    completed = await self._extract(branch_id, context.task.question, cancel)
                    if completed:
                        report.memory_error = getattr(self.memory, "last_extraction_error", None)
                    else:
                        report.memory_status = "cancelled"
                except Exception as exc:
                    # 已提交的回答保留，单独报告同步提取失败。
                    report.memory_error = type(exc).__name__
                if report.memory_status != "cancelled":
                    report.memory_status = "failed" if report.memory_error else "completed"
                forward(
                    Event(
                        "memory_finished",
                        {"status": report.memory_status, "error": report.memory_error},
                    )
                )
        if self.memory is not None and hasattr(self.memory, "maybe_consolidate"):
            if cancel is None or not cancel.is_set():
                try:
                    report.consolidation_scheduled = self.memory.maybe_consolidate()
                    if report.consolidation_scheduled:
                        forward(Event("memory_consolidation_scheduled", {}))
                except Exception as exc:
                    report.consolidation_error = type(exc).__name__
        forward(
            Event("harness_ended", {"status": report.status, "memory_status": report.memory_status})
        )
        return report

    async def _extract(self, branch_id, question, cancel):
        if cancel is None:
            await self.memory.extract(branch_id, question)
            return True
        extraction = asyncio.create_task(self.memory.extract(branch_id, question))
        cancellation = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {extraction, cancellation}, return_when=asyncio.FIRST_COMPLETED
            )
            if extraction in done:
                await extraction
                return True
            return False
        finally:
            for pending in (extraction, cancellation):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(extraction, cancellation, return_exceptions=True)
