"""MD 文档、目录迁移和派生索引。"""

import html
import json
import os
import tempfile
from pathlib import Path

from reading_agent.infrastructure.locking import FileLock
from reading_agent.storage.database import Conflict

from .models import READING_FOLDERS, MemoryDocument


class MemoryDocuments:
    """Memory 的内部文档存储部件；由服务提供 store 和 directory。"""

    def _lock(self):
        return FileLock(self.directory / ".memory.lock")

    @staticmethod
    def _atomic(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write(self, document):
        folder = {
            "profile": "user",
            "user_reading": "user-reading",
            "agent_reading": "agent-reading",
        }[document.category]
        metadata = document.model_dump(exclude={"content"})
        text = "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n\n"
        text += document.content + "\n"
        if len(text.encode("utf-8")) > 256000:
            raise ValueError("memory document too large")
        directory = self.directory / folder
        if document.category in READING_FOLDERS:
            directory /= "entries"
        self._atomic(directory / (document.id + ".md"), text)

    @staticmethod
    def _read_document(path):
        if path.stat().st_size > 256000 or path.is_symlink():
            raise ValueError("invalid memory document size or link")
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n\n" not in text:
            raise ValueError("invalid memory document header")
        header, content = text[4:].split("\n---\n\n", 1)
        return MemoryDocument.model_validate(
            {**json.loads(header), "content": content.rstrip("\n")}
        )

    def _flat_reading_paths(self, folder):
        for path in sorted((self.directory / folder).glob("*.md")):
            if path.name == "memory.md":
                # 旧版允许 ID=memory：仅合法的旧记忆文档需要迁移。
                # 其他内容均视为派生目录，损坏目录不阻塞重建。
                try:
                    document = self._read_document(path)
                except (ValueError, UnicodeError):
                    continue
                if document.id != "memory" or READING_FOLDERS.get(document.category) != folder:
                    continue
            yield path

    def _documents(self):
        documents = {}
        for folder in ("user", "user-reading", "agent-reading"):
            directory = self.directory / folder
            paths = list(directory.glob("*.md"))
            if folder != "user":
                paths = list(self._flat_reading_paths(folder))
                paths += list((directory / "entries").glob("*.md"))
            for path in sorted(paths):
                document = self._read_document(path)
                expected = {
                    "user": "profile",
                    "user-reading": "user_reading",
                    "agent-reading": "agent_reading",
                }[folder]
                if document.id != path.stem or document.category != expected:
                    raise ValueError("memory document identity mismatch")
                if document.id in documents and documents[document.id] != document:
                    raise Conflict("conflicting flat and nested memory documents")
                documents[document.id] = document
        # 新修订本身携带替代关系，单文件原子写即能提交修订。
        # 删除新修订仍保留空正文墓碑，旧版本不会因此重新变成有效记忆。
        replacements = {}
        for document in documents.values():
            if document.replaces:
                if document.replaces in replacements:
                    raise Conflict("multiple memory replacements")
                replacements[document.replaces] = document.id
        for document in documents.values():
            document.superseded_by = replacements.get(document.id, document.superseded_by)
        return documents

    def _migrate_reading_layout(self):
        """调用前先校验所有文档；锁内原子移动旧文件，允许中断后继续。"""
        for folder in READING_FOLDERS.values():
            directory = self.directory / folder
            for path in self._flat_reading_paths(folder):
                target = directory / "entries" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    # _documents 已确认两份内容相同；冲突时不移动或覆盖。
                    path.unlink()
                else:
                    os.replace(path, target)

    @staticmethod
    def _catalog_label(document):
        details = document.details
        title = details.get("title") or details.get("material_id") or document.id
        title = " ".join(str(title).split())[:100]
        if document.category == "user_reading":
            state = {"read": "已读", "interested": "感兴趣", "uninterested": "不感兴趣"}.get(
                details.get("action"), "阅读动作"
            )
        else:
            state = "完整阅读" if details.get("complete") else "部分阅读"
        date = str(details.get("read_at") or document.created_at)[:10]
        label = html.escape(f"{title} · {state} · {date}", quote=False)
        for character in ("\\", "[", "]", "*", "_", "`"):
            label = label.replace(character, "\\" + character)
        return label

    def _write_catalogs(self, documents):
        for category, folder in READING_FOLDERS.items():
            title = "用户阅读动作" if category == "user_reading" else "Agent 阅读记忆"
            entries = sorted(
                (
                    d
                    for d in documents.values()
                    if d.category == category and not d.deleted and not d.superseded_by
                ),
                key=lambda d: (d.updated_at, d.id),
                reverse=True,
            )
            lines = [f"# {title}", ""]
            lines.extend(f"- [{self._catalog_label(d)}](entries/{d.id}.md)" for d in entries)
            text = "\n".join(lines) + "\n"
            path = self.directory / folder / "memory.md"
            # 既避免重复 fsync，也允许无效 UTF-8 目录从权威记录恢复。
            try:
                unchanged = not path.is_symlink() and path.read_text(encoding="utf-8") == text
            except (FileNotFoundError, UnicodeError):
                unchanged = False
            if not unchanged:
                self._atomic(path, text)

    def _migrate(self):
        marker = self.directory / ".legacy-imported"
        if marker.exists():
            return
        existing = self._documents()
        for row in self.store.all("SELECT * FROM memories"):
            if row["id"] in existing:
                continue
            self._write(
                MemoryDocument(
                    id=row["id"],
                    category="profile" if row["kind"] == "profile" else "user_reading",
                    topic_id=row["topic_id"],
                    content=row["content"],
                    origin=row["origin"],
                    evidence=json.loads(row["evidence_refs"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    superseded_by=row["superseded_by"],
                )
            )
        self._atomic(marker, "Imported legacy SQLite memories; Markdown is authoritative.\n")

    @staticmethod
    def _public(document):
        return {
            "id": document.id,
            "kind": "experience" if document.category == "user_reading" else document.category,
            "category": document.category,
            "scope": "topic" if document.topic_id else "global",
            "topic_id": document.topic_id,
            "content": document.content,
            "origin": document.origin,
            "evidence_refs": json.dumps(document.evidence, ensure_ascii=False),
            "superseded_by": document.superseded_by,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
            "details": document.details,
        }

    def _sync(self, *, recover=True):
        if recover:
            from ..memory_consolidation import recover_pending

            recover_pending(self)
        documents = self._documents()
        self._migrate_reading_layout()
        with self.store.transaction() as db:
            db.execute("DELETE FROM memory_documents")
            db.execute("DELETE FROM memories")
            for document in documents.values():
                db.execute(
                    "INSERT INTO memory_documents VALUES(?,?)",
                    (document.id, document.model_dump_json()),
                )
                if document.deleted or document.category == "agent_reading":
                    continue
                row = self._public(document)
                db.execute(
                    "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                    tuple(
                        row[key]
                        for key in (
                            "id",
                            "kind",
                            "scope",
                            "topic_id",
                            "content",
                            "origin",
                            "evidence_refs",
                            "superseded_by",
                            "created_at",
                            "updated_at",
                        )
                    ),
                )
        self._write_catalogs(documents)
        return documents
