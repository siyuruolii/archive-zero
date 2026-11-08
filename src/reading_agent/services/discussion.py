"""任务独立于窗口/SSE；单分支运行、取消收尾、有界草稿与快照。"""

import asyncio
import copy
import time

from reading_agent.agents.harness import Budget, Harness, RunReport
from reading_agent.agents.model_router import stage_provider
from reading_agent.agents.models import Task
from reading_agent.agents.telemetry import run_metrics
from reading_agent.storage.database import Conflict, NotFound

from .run_stream import Running as Running


class Discussion:
    def __init__(self, store, sessions, materials, memory=None):
        self.store, self.sessions, self.materials, self.memory = store, sessions, materials, memory
        self.runs = {}

    async def compact(self, branch_id, *, force=True):
        path = self.sessions.path(branch_id)
        plan = await Harness(self.materials.provider).compact_history(path, force=force)
        if plan is None:
            if force:
                raise Conflict("no complete old rounds to compress")
            return path[-1]["id"]
        return self.sessions.append(branch_id, path[-1]["id"], [("compaction", plan)])

    def start(self, branch_id, question, *, expected_leaf):
        if not question.strip() or len(question) > 8000:
            raise ValueError("question must contain 1–8000 characters")
        if branch_id in self.runs and self.runs[branch_id].phase == "running":
            raise Conflict("branch already running")
        leaf = self.sessions.append(
            branch_id, expected_leaf, [("message", {"role": "user", "content": question})]
        )
        run = Running(provider=self.materials.provider)
        self.sessions.record_event(
            branch_id,
            run.id,
            "run_started",
            {
                "question_entry_id": leaf,
                "model": getattr(stage_provider(run.provider, "discuss"), "model_name", None),
            },
        )
        self.runs[branch_id] = run
        run.task = asyncio.create_task(self._execute(branch_id, leaf, question, run))
        return run.id

    async def stop(self, branch_id):
        run = self.runs.get(branch_id)
        if run and run.phase == "running":
            run.cancel.set()
            if run.task:
                await run.task

    def recovery_info(self, branch_id):
        try:
            info = self.sessions.recovery_state(branch_id)
        except NotFound:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            return {"can_resume": False, "reason": "checkpoint_invalid"}
        return {key: value for key, value in info.items() if key not in {"state", "question"}}

    def resume(self, branch_id, expected_run_id):
        active = self.runs.get(branch_id)
        if active and active.phase == "running":
            raise Conflict("branch already running")
        try:
            info = self.sessions.recovery_state(branch_id)
        except NotFound:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Conflict("checkpoint_invalid") from None
        if not info["can_resume"] or info["run_id"] != expected_run_id:
            raise Conflict(info.get("reason", "recovery_run_changed"))
        run = Running(provider=self.materials.provider)
        run.started = time.monotonic() - info["state"]["report"]["elapsed_seconds"]
        run.status_message = "已从检查点恢复，正在核查状态与继续处理"
        run.metrics = copy.deepcopy(info["state"]["report"].get("observability", {}))
        self.sessions.record_event(
            branch_id,
            run.id,
            "run_started",
            {
                "question_entry_id": info["question_entry_id"],
                "resume_from": expected_run_id,
                "recovery": info["state"],
                "model": getattr(stage_provider(run.provider, "discuss"), "model_name", None),
            },
        )
        self.runs[branch_id] = run
        run.task = asyncio.create_task(
            self._execute(
                branch_id, info["question_entry_id"], info["question"], run, recovery=info["state"]
            )
        )
        return run.id

    async def _execute(self, branch_id, expected_leaf, question, run, *, recovery=None):
        try:
            branch = self.store.one("SELECT * FROM branches WHERE id=?", (branch_id,))
            session = self.store.one("SELECT * FROM sessions WHERE id=?", (branch["session_id"],))
            tm = self.store.one(
                "SELECT * FROM topic_materials WHERE id=?", (session["topic_material_id"],)
            )
            task = Task(
                role="discuss",
                material_id=tm["material_id"],
                topic_ids=[tm["topic_id"]],
                question=question,
            )

            def observe(event):
                self.sessions.record_event(branch_id, run.id, event.kind, event.data)
                if event.kind == "telemetry":
                    run.metrics = event.data
                    run.emit("telemetry", **event.data)
                elif event.kind == "context_compacted":
                    run.emit("status", message="已压缩较早上下文，继续处理")
                elif event.kind == "attempt_started":
                    run.draft = ""
                    run.answer_draft = ""
                    run.emit("snapshot", draft="", answer_draft="", phase=run.phase)
                elif event.kind == "text_delta":
                    run.draft = (run.draft + event.data["text"])[-32000:]
                    run.emit("text_delta", text=event.data["text"])
                elif event.kind == "answer_delta":
                    run.answer_draft += event.data["text"]
                    run.emit("answer_delta", text=event.data["text"], provisional=True)
                elif event.kind == "tool_requested":
                    run.activities.append(
                        {
                            "call_id": event.data["call_id"],
                            "name": event.data["name"],
                            "status": "running",
                        }
                    )
                    run.activities[:] = run.activities[-16:]
                    run.emit("activities", activities=copy.deepcopy(run.activities))
                elif event.kind in {"tool_call_delta", "usage_updated"}:
                    run.emit(event.kind, **event.data)
                elif event.kind in {
                    "memory_finished",
                    "reading_memory_finished",
                    "memory_consolidation_scheduled",
                }:
                    run.emit(event.kind, **event.data)
                elif event.kind == "tool_result":
                    pair = event.data
                    run.tool_pairs.append(pair)
                    for item in run.activities:
                        if item["call_id"] == pair["call_id"]:
                            item["status"] = (
                                pair["result"]["status"] if "result" in pair else "completed"
                            )
                    run.emit("activities", activities=copy.deepcopy(run.activities))
                    run.emit("status", message="已完成阅读工具：" + pair["name"])

            def commit(report):
                run.report = report
                records = [
                    ("message", {"role": "assistant", "kind": "tool_pair", **pair})
                    for pair in report.tools
                ]
                if report.recovery_notes:
                    records.append(
                        (
                            "message",
                            {
                                "role": "assistant",
                                "kind": "recovery_notice",
                                "content": "恢复核查事实："
                                + self.store.dump_json(report.recovery_notes),
                            },
                        )
                    )
                if report.status == "succeeded":
                    answer = report.result["answer"]
                    records.append(
                        (
                            "message",
                            {
                                "role": "assistant",
                                "content": answer,
                                "run_id": run.id,
                                "metrics": copy.deepcopy(report.observability),
                            },
                        )
                    )
                elif report.draft:
                    records.append(
                        (
                            "message",
                            {
                                "role": "assistant",
                                "content": run.answer_draft or report.draft,
                                "incomplete": True,
                                "reason": report.end_reason,
                                "run_id": run.id,
                                "metrics": copy.deepcopy(report.observability),
                            },
                        )
                    )
                if records:
                    leaf = self.sessions.append(branch_id, expected_leaf, records, run_id=run.id)
                    run.emit("message_saved", leaf_id=leaf)

            harness = Harness(
                run.provider,
                lambda prepared: self.materials.corpus(
                    prepared.material_id, prepared.topic_ids, persist=True
                ),
                Budget(context_token_budget=60000),
                memory=self.memory,
                sessions=self.sessions,
            )
            report = None
            if recovery:
                harness.budget = Budget.model_validate(recovery["budget"])
            async for event in harness.stream(
                task,
                cancel=run.cancel,
                on_event=observe,
                commit=commit,
                branch_id=branch_id,
                question_entry_id=expected_leaf,
                recovery=recovery,
            ):
                if event.kind == "result":
                    report = RunReport(**event.data["report"])
            if report is None:
                raise RuntimeError("agent stream ended without a result")
            run.phase = (
                "completed"
                if report.status == "succeeded"
                else "stopped"
                if run.cancel.is_set()
                else "failed"
            )
            run.error = None if report.status == "succeeded" else report.end_reason
        except (NotFound, Conflict):
            run.phase, run.error = "stopped", "讨论已删除或历史末端变化，丢弃晚到结果"
        except Exception as exc:
            # 数据库错误必须独立可见，绝不能发布保存成功。
            run.phase = "stopped" if run.cancel.is_set() else "interrupted"
            run.error = "storage_or_runtime_error:" + type(exc).__name__
        finally:
            if run.report is not None:
                run.metrics = run_metrics(run.report)
            run.metrics = {
                **run.metrics,
                "phase": run.phase,
                "elapsed_seconds": round(time.monotonic() - run.started, 3),
            }
            try:
                self.sessions.record_event(
                    branch_id,
                    run.id,
                    "run_finished",
                    {
                        "status": run.phase,
                        "error": run.error,
                        "attempts": run.report.attempts if run.report else [],
                        "end_reason": run.report.end_reason if run.report else None,
                        "metrics": run.metrics,
                    },
                )
            except NotFound:
                pass  # 删除元数据已终止运行，不恢复已删除的会话。
            except Exception as exc:
                run.phase, run.error = "failed", "event_storage_error:" + type(exc).__name__
            run.emit("status", phase=run.phase, error=run.error)

    def subscribe(self, branch_id):
        run = self.runs.get(branch_id)
        queue = asyncio.Queue(maxsize=128)
        if run:
            # 同一事件循环中无 await，快照与订阅注册之间无丢失空窗。
            run.subscribers.add(queue)
        return run, queue, run.snapshot() if run else None

    async def shutdown(self):
        await asyncio.gather(*(self.stop(bid) for bid in list(self.runs)))
