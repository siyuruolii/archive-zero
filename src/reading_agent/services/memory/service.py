"""三层记忆：工作上下文接入、MD 长期记忆、关键词与模型辅助会话回忆。"""

import hashlib
import json
import re
from pathlib import Path

from reading_agent.agents.harness import Harness
from reading_agent.storage.database import Conflict, NotFound, identity, now

from ..sessions import Sessions
from .documents import MemoryDocuments
from .models import Extraction, MemoryDocument
from .recall import SessionRecall

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('profile','experience')),
 scope TEXT NOT NULL CHECK(scope IN ('topic','global')),topic_id TEXT,content TEXT NOT NULL,
 origin TEXT NOT NULL,evidence_refs TEXT NOT NULL,superseded_by TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 CHECK((scope='global' AND topic_id IS NULL) OR (scope='topic' AND topic_id IS NOT NULL)));
CREATE INDEX IF NOT EXISTS memory_scope ON memories(scope,topic_id,superseded_by);
"""


class Memory(MemoryDocuments, SessionRecall):
    def __init__(
        self,
        store,
        provider,
        *,
        directory=None,
        extraction_provider=None,
        consolidation_provider=None,
        consolidation_policy=None,
    ):
        self.store, self.provider = store, provider
        self.extraction_provider = extraction_provider
        folder = "memories" if store.path.name == "reading.db" else store.path.stem + "-memories"
        self.directory = Path(directory or store.path.parent / folder)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_extraction_error = None
        with store.transaction() as db:
            db.executescript(SCHEMA)
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_documents("
                "id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
        with self._lock():
            self._migrate()
            self._sync()
        self.rebuild_sessions()
        from ..memory_consolidation import MemoryConsolidator

        self.consolidator = MemoryConsolidator(self, consolidation_provider, consolidation_policy)

    def rebuild(self):
        with self._lock():
            return len(self._sync())

    def add(
        self,
        content,
        *,
        topic_id=None,
        kind="profile",
        origin="user_statement",
        evidence=(),
        details=None,
    ):
        category = {
            "profile": "profile",
            "experience": "user_reading",
            "user_reading": "user_reading",
            "agent_reading": "agent_reading",
        }.get(kind)
        if (
            not content.strip()
            or len(content) > (16000 if category == "agent_reading" else 2000)
            or category is None
        ):
            raise ValueError("invalid memory")
        with self._lock():
            documents = self._sync()
            if topic_id:
                self.store.one("SELECT id FROM topics WHERE id=?", (topic_id,))
            for ref in evidence:
                table = {"entry": "entries", "topic_material": "topic_materials"}.get(
                    ref.get("type")
                )
                if table:
                    self.store.one(f"SELECT id FROM {table} WHERE id=?", (ref["id"],))
            for old in documents.values():
                if (
                    not old.deleted
                    and not old.superseded_by
                    and old.category == category
                    and old.content == content.strip()
                    and old.topic_id == topic_id
                    and old.evidence == list(evidence)
                    and old.details == (details or {})
                ):
                    return old.id
            document = MemoryDocument(
                id=identity(),
                category=category,
                topic_id=topic_id,
                content=content.strip(),
                origin=origin,
                evidence=list(evidence),
                details=details or {},
                created_at=now(),
                updated_at=now(),
            )
            self._write(document)
            self._sync()
            return document.id

    def list(self, topic_id=None, *, query="", limit=8, category=None):
        if topic_id and not self.store.all("SELECT id FROM topics WHERE id=?", (topic_id,)):
            return []
        with self._lock():
            documents = self._sync()
        rows = [
            self._public(d)
            for d in documents.values()
            if not d.deleted
            and not d.superseded_by
            and d.topic_id in (None, topic_id)
            and (category is None or d.category == category)
        ]
        words = re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", query.casefold())

        def score(row):
            return sum(word in row["content"].casefold() for word in words)

        rows.sort(key=lambda r: (score(r), r["updated_at"], r["id"]), reverse=True)
        if query:
            rows = [r for r in rows if score(r)]
        return rows[: max(1, min(limit, 20))]

    def search_entries(self, topic_id, query):
        return [
            {
                "memory_id": r["id"],
                "category": r["category"],
                "topic_id": r["topic_id"],
                "preview": r["content"][:240],
                "updated_at": r["updated_at"],
            }
            for r in self.list(topic_id, query=query, limit=5)
        ]

    def read_memory(self, memory_id, *, topic_id):
        if topic_id and not self.store.all("SELECT id FROM topics WHERE id=?", (topic_id,)):
            return None
        with self._lock():
            document = self._sync().get(memory_id)
        if (
            document is None
            or document.deleted
            or document.superseded_by
            or document.topic_id not in (None, topic_id)
        ):
            return None
        return self._public(document)

    def correct(self, memory_id, content):
        if not content.strip() or len(content) > 2000:
            raise ValueError("invalid correction")
        with self._lock():
            old = self._sync().get(memory_id)
            if old is None or old.deleted:
                raise NotFound("memory not found")
            if old.superseded_by:
                raise Conflict("memory already replaced")
            new = old.model_copy(
                update={
                    "id": identity(),
                    "content": content.strip(),
                    "origin": "user_statement",
                    "evidence": [],
                    "replaces": old.id,
                    "superseded_by": None,
                    "created_at": now(),
                    "updated_at": now(),
                }
            )
            self._write(new)
            self._sync()
            return new.id

    def delete(self, memory_id):
        with self._lock():
            old = self._sync().get(memory_id)
            if old is None or old.deleted:
                raise NotFound("memory not found")
            self._write(
                old.model_copy(
                    update={
                        "deleted": True,
                        "content": "",
                        "evidence": [],
                        "details": {},
                        "updated_at": now(),
                    }
                )
            )
            self._sync()

    def evidence(self, memory_id):
        with self._lock():
            document = self._sync().get(memory_id)
        if document is None or document.deleted:
            raise NotFound("memory not found")
        results = []
        for ref in document.evidence:
            table = {
                "entry": "entries",
                "topic_material": "topic_materials",
                "material": "materials",
            }.get(ref.get("type"))
            accessible = bool(
                table and self.store.all(f"SELECT id FROM {table} WHERE id=?", (ref.get("id"),))
            )
            if accessible and ref.get("type") == "entry":
                try:
                    Sessions(self.store, initialize=False).read_entry(ref["id"])
                except (NotFound, Conflict):
                    accessible = False
            results.append(
                {
                    **ref,
                    "accessible": accessible,
                    "message": "依据可访问" if accessible else "依据已不可访问",
                }
            )
        return results

    async def extract(self, branch_id, question):
        try:
            branch = self.store.one("SELECT * FROM branches WHERE id=?", (branch_id,))
            session = self.store.one("SELECT * FROM sessions WHERE id=?", (branch["session_id"],))
            tm = self.store.one(
                "SELECT * FROM topic_materials WHERE id=?", (session["topic_material_id"],)
            )
            entries = list(
                reversed(Sessions(self.store, initialize=False).all_entries(session["id"]))
            )
            # 限制在当前叶的祖先路径，不能拿其他分支的同名问题当证据。
            by_id = {entry["id"]: entry for entry in entries}
            path_ids = set()
            leaf = branch["leaf_entry_id"]
            while leaf is not None:
                if leaf in path_ids or leaf not in by_id:
                    raise Conflict("invalid extraction ancestry")
                path_ids.add(leaf)
                leaf = by_id[leaf]["parent_id"]
            user_entry = next(
                (
                    e
                    for e in entries
                    if e["id"] in path_ids
                    and json.loads(e["data"]).get("role") == "user"
                    and json.loads(e["data"]).get("content") == question
                ),
                None,
            )
            if user_entry is None:
                return
            extracted = await Harness(self.extraction_provider or self.provider).infer(
                Extraction,
                "仅从真实用户发言抽取明确长期背景或偏好，不从提问推断已掌握。不要提炼文章/助手的陈述。"
                "quote逐字引用支持背景的用户原话。仅用户明确说所有主题/全局/通用时explicit_global为真。没有则profiles为空。",
                {"user_message": question},
                stage="extract",
            )
            # await 后再次检查原身份，不允许删讨论之后晚到新增记忆。
            with self.store.transaction() as db:
                self.store.require(db, "branches", branch_id)
                self.store.require(db, "entries", user_entry["id"])
                self.store.require(db, "topics", tm["topic_id"])
            current = Sessions(self.store, initialize=False).read_entry(user_entry["id"])
            current_data = json.loads(current["data"])
            if current_data.get("role") != "user" or current_data.get("content") != question:
                raise Conflict("extraction source changed")
            for candidate in extracted.profiles:
                if candidate.quote not in question:
                    continue
                global_allowed = candidate.explicit_global and any(
                    x in candidate.quote for x in ("全局", "所有主题", "通用")
                )
                self.add(
                    candidate.content,
                    topic_id=None if global_allowed else tm["topic_id"],
                    evidence=[{"type": "entry", "id": user_entry["id"]}],
                )
            self.last_extraction_error = None
        except Exception as exc:
            # 提炼失败不回滚已保存讨论；向管理入口暴露错误类型，避免静默伪成功。
            self.last_extraction_error = type(exc).__name__

    def maybe_consolidate(self, *, force=False):
        return self.consolidator.maybe_schedule(force=force)

    async def shutdown(self):
        await self.consolidator.shutdown()

    def experience(self, tm_id, action):
        if action not in {"read", "interested", "uninterested"}:
            raise ValueError("unsupported business event")
        row = self.store.one("SELECT * FROM topic_materials WHERE id=?", (tm_id,))
        labels = {
            "read": "用户曾明确标记该材料已读；不代表读完全文或掌握知识。",
            "interested": "用户曾标记该材料感兴趣。",
            "uninterested": "用户曾标记该材料不感兴趣。",
        }
        return self.add(
            labels[action],
            topic_id=row["topic_id"],
            kind="experience",
            origin="business_event",
            evidence=[{"type": "topic_material", "id": tm_id}],
            details={"action": action, "material_id": row["material_id"]},
        )

    @staticmethod
    def _merge_ranges(ranges):
        merged = []
        for start, end in sorted(ranges, key=lambda pair: (pair[0], pair[1])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        return merged

    def remember_readings(self, task, corpus, report):
        """只依据实际工具凭据记录阅读；旧记忆不能伪造本次精读覆盖。"""
        grouped = {}
        for read in report.readings:
            grouped.setdefault(read["material_id"], []).append((read["start"], read["end"]))
        saved = []
        for material_id, ranges in grouped.items():
            material = corpus.materials.get(material_id)
            if material is None or not material.body:
                continue
            body = material.body
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            rows = self.store.all("SELECT published_at FROM materials WHERE id=?", (material_id,))
            for topic_id in task.topic_ids:
                with self._lock():
                    documents = self._sync()
                    document = next(
                        (
                            d
                            for d in documents.values()
                            if not d.deleted
                            and not d.superseded_by
                            and d.category == "agent_reading"
                            and d.topic_id == topic_id
                            and d.details.get("material_id") == material_id
                            and d.details.get("content_hash") == digest
                        ),
                        None,
                    )
                    previous = document.details.get("coverage", []) if document else []
                    coverage = self._merge_ranges(previous + ranges)
                    if any(start < 0 or end > len(body) or start >= end for start, end in coverage):
                        raise ValueError("reading evidence outside actual body")
                    excerpt = "\n".join(
                        body[start : min(end, start + 600)] for start, end in coverage
                    )[:1800]
                    summary = ""
                    if (
                        report.status == "succeeded"
                        and task.role == "deep_read"
                        and material_id == task.material_id
                    ):
                        summary = report.result.get("deep_summary", "")[:6000]
                    elif document:
                        summary = document.details.get("summary", "")
                    details = {
                        "material_id": material_id,
                        "title": material.title,
                        "url": material.url,
                        "content_hash": digest,
                        "coverage": coverage,
                        "total_chars": len(body),
                        "complete": coverage == [[0, len(body)]],
                        "excerpt": excerpt,
                        "summary": summary,
                        "published_at": rows[0]["published_at"] if rows else None,
                        "read_at": now(),
                        "freshness": "historical_snapshot_requires_revalidation",
                    }
                    content = (
                        f"Agent 阅读过：{material.title}\n内容版本 SHA256：{digest}\n"
                        f"实际覆盖：{coverage}；完整：{details['complete']}\n"
                        "这是历史阅读记录，不代表用户已读或掌握；时效事实需要重新核实。\n"
                        + ("阅读摘要：" + summary if summary else "已读原文摘录：" + excerpt)
                    )
                    if document is None:
                        document = MemoryDocument(
                            id=identity(),
                            category="agent_reading",
                            topic_id=topic_id,
                            content=content,
                            origin="verified_tool_read",
                            created_at=now(),
                            updated_at=now(),
                            evidence=[{"type": "material", "id": material_id}],
                            details=details,
                        )
                    else:
                        document = document.model_copy(
                            update={"content": content, "details": details, "updated_at": now()}
                        )
                    self._write(document)
                    self._sync()
                    saved.append(document.id)
        return saved

    def attach(self, corpus, task):
        corpus.memory_loader = self.search_entries
        corpus.memory_reader = self.read_memory
        corpus.memories = {}
        for tid in task.topic_ids:
            query = task.question or task.short_summary
            profiles = self.list(tid, query=query, category="profile", limit=3)
            if not profiles:
                profiles = self.list(tid, category="profile", limit=2)
            actions = self.list(tid, query=query, category="user_reading", limit=2)
            corpus.memories[tid] = profiles + actions
        with self._lock():
            documents = self._sync()
        body = corpus.materials[task.material_id].body
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None
        corpus.reading_memories = {}
        for tid in task.topic_ids:
            candidates = sorted(
                (
                    d
                    for d in documents.values()
                    if d.category == "agent_reading"
                    and not d.deleted
                    and not d.superseded_by
                    and d.topic_id in (None, tid)
                    and d.details.get("material_id") == task.material_id
                ),
                key=lambda d: d.updated_at,
                reverse=True,
            )[:2]
            corpus.reading_memories[tid] = [
                {
                    **self._public(d),
                    "current_version_verified": bool(
                        digest and d.details.get("content_hash") == digest
                    ),
                }
                for d in candidates
            ]
