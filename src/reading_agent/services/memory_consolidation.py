"""后台沉淀：条件调度、隔离工作副本、乐观提交、崩溃后提交恢复。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.model_router import stage_provider
from reading_agent.agents.models import Material, StrictModel, Task, Topic
from reading_agent.agents.tool import Corpus
from reading_agent.infrastructure.locking import FileLock
from reading_agent.storage.database import Conflict, identity, now

from .memory import READING_FOLDERS, MemoryDocument


class ConsolidationPolicy(StrictModel):
    enabled: bool = True
    min_new: int = Field(default=20, ge=1)
    cooldown_seconds: float = Field(default=1800.0, ge=0)
    interval_seconds: float = Field(default=86400.0, gt=0)
    batch_size: int = Field(default=10, ge=1, le=30)
    context_size: int = Field(default=5, ge=0, le=20)
    max_model_calls: int = Field(default=20, ge=1, le=50)
    max_seconds: float = Field(default=180.0, gt=0, le=600)


def relative_path(document):
    if document.category == "profile":
        return f"user/{document.id}.md"
    return f"{READING_FOLDERS[document.category]}/entries/{document.id}.md"


def render(document):
    return (
        "---\n"
        + json.dumps(document.model_dump(exclude={"content"}), ensure_ascii=False, indent=2)
        + "\n---\n\n"
        + document.content
        + "\n"
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def canonical_path(memory, relative):
    path = memory.directory / relative
    if path.is_symlink() or not path.resolve().is_relative_to(memory.directory.resolve()):
        raise ValueError("invalid consolidation commit path")
    return path


def state_path(memory):
    return memory.directory / ".consolidation" / "state.json"


def load_state(memory):
    path = state_path(memory)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("processed_ids"), list):
            raise ValueError("invalid consolidation state")
        return state
    return {
        "version": 1,
        "status": "idle",
        "created_at": now(),
        "processed_ids": [],
        "last_success_at": None,
        "last_attempt_at": None,
        "run_id": None,
        "error": None,
    }


def save_state(memory, state):
    memory._atomic(state_path(memory), json.dumps(state, ensure_ascii=False, indent=2))


def recover_pending(memory):
    """调用者必须持有 .memory.lock；提交记录存在时先完成恢复，再向读者暴露记忆。"""
    journal = memory.directory / ".consolidation" / "commit.json"
    if not journal.exists():
        return
    plan = json.loads(journal.read_text(encoding="utf-8"))
    if plan.get("version") != 1:
        raise ValueError("invalid consolidation commit")
    updates = [MemoryDocument.model_validate(d) for d in plan["documents"]]
    texts = {relative_path(d): render(d) for d in updates}
    final_hashes = {p: hashlib.sha256(t.encode("utf-8")).hexdigest() for p, t in texts.items()}
    # 路径只能由经过模型校验的记忆 ID/类别生成，绝不直接使用 Agent 的路径。
    for path, expected in plan["expected"].items():
        parts = Path(path).parts
        allowed = (len(parts) == 2 and parts[0] == "user") or (
            len(parts) == 3 and parts[0] in READING_FOLDERS.values() and parts[1] == "entries"
        )
        if not allowed or any(p in ("..", ".") for p in parts) or not path.endswith(".md"):
            raise ValueError("invalid consolidation commit path")
        target = canonical_path(memory, path)
        allowed_hashes = {expected}
        if path in final_hashes:
            allowed_hashes.add(final_hashes[path])
        if expected is None or digest(target) not in allowed_hashes:
            raise Conflict("consolidation recovery source changed")
    current_documents = memory._documents()
    planned = {d.id: d for d in updates}
    for mid, expected in plan.get("expected_superseded", {}).items():
        current = current_documents.get(mid)
        permitted = {expected}
        if mid in planned:
            permitted.add(planned[mid].superseded_by)
        if current is None or current.superseded_by not in permitted:
            raise Conflict("consolidation recovery source replaced")
    for path, text in texts.items():
        target = canonical_path(memory, path)
        if (
            path not in plan["expected"]
            and target.exists()
            and digest(target) != final_hashes[path]
        ):
            raise Conflict("consolidation destination exists")
        memory._atomic(target, text)
    memory._sync(recover=False)
    state = load_state(memory)
    state.update(
        status="completed",
        error=None,
        run_id=plan["run_id"],
        last_success_at=plan["committed_at"],
        processed_ids=sorted(set(state["processed_ids"]) | set(plan["reviewed_ids"])),
    )
    save_state(memory, state)
    journal.unlink()


class MemoryWorkspace:
    """Agent 能力对象：可访问清单内文件的工作副本，不能写正式目录。"""

    def __init__(self, memory, run_id, documents, pending_ids):
        self.memory = memory
        self.run_id = run_id
        self.root = memory.directory / ".consolidation" / "runs" / run_id
        self.pending_ids = set(pending_ids)
        self.sources = {}
        self.expected = {}
        self.read_ids = set()
        self.created = {}
        for document in documents:
            path = relative_path(document)
            canonical = memory.directory / path
            self.expected[path] = digest(canonical)
            self.sources[path] = memory._read_document(canonical)
            for directory in ("before", "work"):
                memory._atomic(self.root / directory / path, canonical.read_text(encoding="utf-8"))

    def __deepcopy__(self, memo):
        # 保留能力对象身份，资料 DTO 仍由 Harness 深拷贝。
        return self

    def manifest(self):
        return [
            {
                "path": p,
                "id": d.id,
                "category": d.category,
                "topic_id": d.topic_id,
                "pending": d.id in self.pending_ids,
                "preview": d.content[:160],
            }
            for p, d in self.sources.items()
        ]

    def _path(self, path):
        if path not in self.sources and path not in self.created:
            raise ValueError("workspace_path_not_allowed")
        target = self.root / "work" / path
        if target.is_symlink() or not target.resolve().is_relative_to(
            (self.root / "work").resolve()
        ):
            raise ValueError("workspace_path_not_allowed")
        return target

    def read(self, paths):
        records = []
        size = 0
        for path in paths:
            target = self._path(path)
            text = target.read_text(encoding="utf-8")
            size += len(text.encode("utf-8"))
            if size > 60000:
                raise ValueError("workspace_read_budget_exhausted")
            records.append({"path": path, "text": text})
        self.read_ids.update(self.sources[p].id for p in paths if p in self.sources)
        return records

    def _validate(self, path, document, *, require_read=True):
        original = self.sources.get(path) or self.created.get(path)
        if document.id != original.id or relative_path(document) != path:
            raise ValueError("workspace_identity_changed")
        allowed = {"content", "superseded_by"}
        if document.model_dump(exclude=allowed) != original.model_dump(exclude=allowed):
            raise ValueError("workspace_metadata_changed")
        if not document.content.strip():
            raise ValueError("workspace_empty_memory")
        if original.category == "user_reading" and original.origin != "memory_consolidation":
            if document != original:
                raise ValueError("workspace_reading_event_immutable")
        if require_read and original.id not in self.read_ids and path in self.sources:
            raise ValueError("workspace_read_before_write")

    def write(self, path, text):
        target = self._path(path)
        if len(text.encode("utf-8")) > 256000:
            raise ValueError("workspace_file_too_large")
        if not text.startswith("---\n") or "\n---\n\n" not in text:
            raise ValueError("workspace_invalid_header")
        header, content = text[4:].split("\n---\n\n", 1)
        document = MemoryDocument.model_validate(
            {**json.loads(header), "content": content.rstrip("\n")}
        )
        self._validate(path, document)
        self.memory._atomic(target, text)
        return {"path": path, "written": True}

    def patch(self, path, old_text, new_text):
        text = self._path(path).read_text(encoding="utf-8")
        if not old_text or text.count(old_text) != 1:
            raise ValueError("workspace_patch_not_unique")
        return self.write(path, text.replace(old_text, new_text, 1))

    def create(self, category, topic_id, source_ids, content):
        by_id = {d.id: d for d in self.sources.values()}
        if not source_ids or any(i not in by_id or i not in self.read_ids for i in source_ids):
            raise ValueError("workspace_invalid_sources")
        sources = [by_id[i] for i in source_ids]
        if topic_id not in {d.topic_id for d in sources} or any(
            d.category != category or d.topic_id not in (None, topic_id) for d in sources
        ):
            raise ValueError("workspace_source_scope_mismatch")
        document = MemoryDocument(
            id=identity(),
            category=category,
            topic_id=topic_id,
            content=content,
            origin="memory_consolidation",
            created_at=now(),
            updated_at=now(),
            evidence=list(
                {json.dumps(e, sort_keys=True): e for d in sources for e in d.evidence}.values()
            ),
            details={
                "source_ids": list(dict.fromkeys(source_ids)),
                "consolidation_run": self.run_id,
            },
        )
        path = relative_path(document)
        self.created[path] = document
        self.memory._atomic(self._path(path), render(document))
        return {"path": path, "id": document.id}

    def plan(self):
        if not self.pending_ids <= self.read_ids:
            raise ValueError("consolidation_pending_not_reviewed")
        documents = {}
        updates = []
        for path in self.sources.keys() | self.created.keys():
            document = self.memory._read_document(self._path(path))
            self._validate(path, document, require_read=(document != self.sources.get(path)))
            documents[document.id] = document
            if path in self.created or document != self.sources[path]:
                updates.append(
                    document.model_copy(
                        update={"origin": "memory_consolidation", "updated_at": now()}
                    )
                )
        for document in documents.values():
            seen = {document.id}
            target = document.superseded_by
            while target:
                other = documents.get(target)
                if (
                    target in seen
                    or other is None
                    or other.deleted
                    or (other.category != document.category or other.topic_id != document.topic_id)
                ):
                    raise ValueError("consolidation_invalid_merge")
                seen.add(target)
                target = other.superseded_by
        return {
            "version": 1,
            "run_id": self.run_id,
            "committed_at": now(),
            "expected": self.expected,
            "expected_superseded": {d.id: d.superseded_by for d in self.sources.values()},
            "reviewed_ids": sorted(self.pending_ids),
            "documents": [d.model_dump() for d in updates],
        }


class MemoryConsolidator:
    def __init__(self, memory, provider=None, policy=None):
        self.memory = memory
        self.provider = provider
        self.policy = policy or ConsolidationPolicy()
        self.task = None
        self.runtime_error = None
        with memory._lock():
            state = load_state(memory)
            # 后台所有权锁不保护记忆写入，仅用于跨进程 single-flight 和崩溃判定。
            try:
                with FileLock(memory.directory / ".consolidation" / "runner.lock"):
                    if state["status"] == "running":
                        state.update(status="interrupted", error="process_interrupted")
            except Conflict:
                pass
            save_state(memory, state)

    def status(self):
        with self.memory._lock():
            documents = self.memory._sync()
            state = load_state(self.memory)
            pending = self._pending(documents, state)
            return {
                **state,
                "processed_count": len(state["processed_ids"]),
                "pending_count": len(pending),
                "policy": self.policy.model_dump(),
                "model": getattr(
                    stage_provider(self.provider or self.memory.provider, "consolidate"),
                    "model_name",
                    None,
                ),
                "runtime_error": self.runtime_error,
            }

    def _pending(self, documents, state):
        processed = set(state["processed_ids"])
        topics = {r["id"] for r in self.memory.store.all("SELECT id FROM topics")}
        return sorted(
            (
                d
                for d in documents.values()
                if not d.deleted
                and not d.superseded_by
                and d.origin != "memory_consolidation"
                and d.id not in processed
                and (d.topic_id is None or d.topic_id in topics)
            ),
            key=lambda d: (d.created_at, d.id),
        )

    def maybe_schedule(self, *, force=False):
        if not self.policy.enabled or (self.task is not None and not self.task.done()):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        lease = FileLock(self.memory.directory / ".consolidation" / "runner.lock")
        run_id = None
        try:
            with self.memory._lock():
                documents = self.memory._sync()
                state = load_state(self.memory)
                pending = self._pending(documents, state)
                current = datetime.now(UTC)
                since_success = current - datetime.fromisoformat(
                    state["last_success_at"] or state["created_at"]
                )
                cooled = (
                    not state["last_attempt_at"]
                    or (current - datetime.fromisoformat(state["last_attempt_at"])).total_seconds()
                    >= self.policy.cooldown_seconds
                )
                if not pending or (
                    not force
                    and (
                        not cooled
                        or not (
                            len(pending) >= self.policy.min_new
                            or since_success.total_seconds() >= self.policy.interval_seconds
                        )
                    )
                ):
                    return False
                lease.__enter__()
                run_id = identity()
                batch = pending[: self.policy.batch_size]
                groups = {(d.category, d.topic_id) for d in batch}
                ids = {d.id for d in batch}
                context = [
                    d
                    for d in documents.values()
                    if d.id not in ids
                    and not d.deleted
                    and not d.superseded_by
                    and (d.category, d.topic_id) in groups
                ][: self.policy.context_size]
                state.update(status="running", run_id=run_id, error=None, last_attempt_at=now())
                save_state(self.memory, state)
                workspace = MemoryWorkspace(self.memory, run_id, batch + context, ids)
            self.task = loop.create_task(self._run(workspace, lease))
            self.runtime_error = None

            def finished(task):
                try:
                    if task.cancelled():
                        with self.memory._lock():
                            state = load_state(self.memory)
                            if state["status"] == "running" and state["run_id"] == run_id:
                                state.update(status="interrupted", error="cancelled_before_start")
                                save_state(self.memory, state)
                    elif task.exception() is not None:
                        self.runtime_error = type(task.exception()).__name__
                finally:
                    lease.__exit__(None, None, None)

            self.task.add_done_callback(finished)
            return True
        except Conflict:
            lease.__exit__(None, None, None)
            if run_id is not None:
                self._failure(run_id, "conflict", "snapshot_conflict")
            return False
        except BaseException as exc:
            lease.__exit__(None, None, None)
            if run_id is not None:
                self._failure(run_id, "failed", type(exc).__name__)
            raise

    async def _run(self, workspace, lease):
        try:
            corpus = Corpus(
                topics={"memory-maintenance": Topic(id="memory-maintenance", name="记忆整理")},
                materials={
                    "memory-workspace": Material(id="memory-workspace", title="后台记忆工作副本")
                },
                memory_workspace=workspace,
            )
            report = await Harness(
                self.provider or self.memory.provider,
                corpus,
                Budget(
                    max_model_calls=self.policy.max_model_calls,
                    max_tool_calls=30,
                    max_seconds=self.policy.max_seconds,
                ),
            ).run(
                Task(
                    role="consolidate",
                    material_id="memory-workspace",
                    topic_ids=["memory-maintenance"],
                    question="读取本批pending文件后执行有依据的记忆整理；使用文件工具，完成后返回结果。",
                )
            )
            self.memory._atomic(
                workspace.root / "report.json",
                json.dumps(
                    {
                        "status": report.status,
                        "end_reason": report.end_reason,
                        "attempts": report.attempts,
                        "tools": [
                            {"name": t["name"], "status": t["result"]["status"]}
                            for t in report.tools
                        ],
                        "result": report.result,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            if report.status != "succeeded":
                raise ValueError("consolidation_agent_" + report.end_reason)
            plan = workspace.plan()
            with self.memory._lock():
                current = self.memory._documents()
                for mid, expected in plan["expected_superseded"].items():
                    if mid not in current or current[mid].superseded_by != expected:
                        raise Conflict("consolidation_source_replaced")
                    topic_id = current[mid].topic_id
                    if topic_id and not self.memory.store.all(
                        "SELECT id FROM topics WHERE id=?", (topic_id,)
                    ):
                        raise Conflict("consolidation_topic_deleted")
                for path, expected in plan["expected"].items():
                    if digest(self.memory.directory / path) != expected:
                        raise Conflict("consolidation_source_changed")
                journal = self.memory.directory / ".consolidation" / "commit.json"
                self.memory._atomic(journal, json.dumps(plan, ensure_ascii=False, indent=2))
                recover_pending(self.memory)
        except asyncio.CancelledError:
            self._failure(workspace.run_id, "interrupted", "cancelled")
            raise
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, (ValueError, Conflict)) and str(exc).startswith("consolidation_")
                else type(exc).__name__
            )
            self._failure(
                workspace.run_id, "conflict" if isinstance(exc, Conflict) else "failed", code
            )
        finally:
            lease.__exit__(None, None, None)

    def _failure(self, run_id, status, error):
        with self.memory._lock():
            state = load_state(self.memory)
            state.update(status=status, run_id=run_id, error=error)
            save_state(self.memory, state)
            self.memory._atomic(
                self.memory.directory / ".consolidation" / "runs" / run_id / "error.json",
                json.dumps(
                    {"run_id": run_id, "status": status, "error": error, "time": now()}, indent=2
                ),
            )

    async def shutdown(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
