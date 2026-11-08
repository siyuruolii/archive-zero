"""JSONL 会话事实流，SQLite 只保存结构/位置/运行元数据和可重建全文索引。"""

import hashlib
import json
import os
import re
from pathlib import Path

from reading_agent.infrastructure.locking import FileLock
from reading_agent.storage.database import Conflict, identity, now

MAX_EVENT_BYTES = 16 * 1024 * 1024
ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_logs(
 session_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, indexed_bytes INTEGER NOT NULL DEFAULT 0,
 last_sequence INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
 tombstone_event_id TEXT, created_at TEXT NOT NULL, recovery_note TEXT);
CREATE TABLE IF NOT EXISTS session_events(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session_logs(session_id),
 sequence INTEGER NOT NULL,type TEXT NOT NULL,branch_id TEXT,run_id TEXT,created_at TEXT NOT NULL,
 byte_offset INTEGER NOT NULL,byte_length INTEGER NOT NULL,sha256 TEXT NOT NULL,
 UNIQUE(session_id,sequence));
CREATE INDEX IF NOT EXISTS session_event_run ON session_events(run_id,sequence);
CREATE TABLE IF NOT EXISTS session_runs(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES session_logs(session_id),
 branch_id TEXT,
 status TEXT NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,error TEXT,
 last_sequence INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS session_log_deleted AFTER DELETE ON sessions BEGIN
 UPDATE session_logs SET deleted=1 WHERE session_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS session_runs_deleted AFTER DELETE ON sessions BEGIN
 UPDATE session_runs SET status='cancelled',ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
 error='session_deleted' WHERE session_id=OLD.id AND status='running';
END;
"""


class SessionEvents:
    def __init__(self, store, directory=None):
        self.store = store
        folder = "sessions" if store.path.name == "reading.db" else store.path.stem + "-sessions"
        self.directory = Path(directory or store.path.parent / folder)
        self.directory.mkdir(parents=True, exist_ok=True)

    def lock(self):
        return FileLock(self.directory / ".sessions.lock")

    def path(self, sid):
        if not ID_PATTERN.fullmatch(sid):
            raise ValueError("invalid session identity")
        path = self.directory / (sid + ".jsonl")
        if path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve()):
            raise Conflict("invalid session log path")
        return path

    def initialize(self, db):
        columns = {r["name"] for r in db.execute("PRAGMA table_info(entries)")}
        for name, kind in (("event_id", "TEXT"), ("event_index", "INTEGER")):
            if name not in columns:
                db.execute(f"ALTER TABLE entries ADD COLUMN {name} {kind}")
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        # 旧触发器从 entries.data 提取正文；现在由事件投影更新 FTS。
        for suffix in ("words", "trigrams"):
            for operation in ("insert", "update"):
                db.execute(f"DROP TRIGGER IF EXISTS memory_{suffix}_{operation}")
        for path in sorted(self.directory.glob("*.jsonl")):
            self.recover(db, path.stem)
        for row in list(db.execute("SELECT * FROM sessions")):
            self.migrate(db, dict(row))
        self.flush_deleted(db)

    @staticmethod
    def metadata(data):
        return {k: data[k] for k in ("role", "kind", "incomplete") if k in data}

    def event(self, db, sid, kind, data, *, branch_id=None, run_id=None):
        row = db.execute("SELECT * FROM session_logs WHERE session_id=?", (sid,)).fetchone()
        return {
            "version": 1,
            "id": identity(),
            "session_id": sid,
            "sequence": (row["last_sequence"] if row else 0) + 1,
            "type": kind,
            "branch_id": branch_id,
            "run_id": run_id,
            "created_at": now(),
            "data": data,
        }

    def ensure_log(self, db, event):
        self.path(event["session_id"])
        db.execute(
            "INSERT OR IGNORE INTO session_logs(session_id,file_name,created_at) VALUES(?,?,?)",
            (event["session_id"], event["session_id"] + ".jsonl", event["created_at"]),
        )

    def append(self, db, event):
        raw = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("session event exceeds byte budget")
        path = self.path(event["session_id"])
        expected = db.execute(
            "SELECT indexed_bytes FROM session_logs WHERE session_id=?", (event["session_id"],)
        ).fetchone()[0]
        with path.open("ab") as stream:
            offset = stream.tell()
            if offset != expected:
                raise Conflict("session log requires recovery")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return offset, raw

    def persist(self, db, event):
        self.ensure_log(db, event)
        self.apply(db, event)
        offset, raw = self.append(db, event)
        self.index(db, event, offset, raw)

    def index(self, db, event, offset, raw):
        db.execute(
            "INSERT INTO session_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                event["id"],
                event["session_id"],
                event["sequence"],
                event["type"],
                event.get("branch_id"),
                event.get("run_id"),
                event["created_at"],
                offset,
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        db.execute(
            "UPDATE session_logs SET indexed_bytes=?,last_sequence=? WHERE session_id=?",
            (offset + len(raw), event["sequence"], event["session_id"]),
        )
        kind = event["type"]
        if kind in {"session_deleted", "branch_deleted"}:
            clause = "session_id=?" if kind == "session_deleted" else "branch_id=?"
            target = event["session_id"] if kind == "session_deleted" else event["branch_id"]
            db.execute(
                f"UPDATE session_runs SET status='cancelled',ended_at=? "
                f"WHERE {clause} AND status='running'",
                (event["created_at"], target),
            )
        if kind == "agent.run_started":
            db.execute(
                "INSERT INTO session_runs VALUES(?,?,?,'running',?,NULL,NULL,?)",
                (
                    event["run_id"],
                    event["session_id"],
                    event["branch_id"],
                    event["created_at"],
                    event["sequence"],
                ),
            )
        elif event.get("run_id"):
            db.execute(
                "UPDATE session_runs SET last_sequence=? WHERE id=?",
                (event["sequence"], event["run_id"]),
            )
            if kind in {"agent.run_finished", "agent.run_interrupted"}:
                db.execute(
                    "UPDATE session_runs SET status=?,ended_at=?,error=? WHERE id=?",
                    (
                        event["data"].get("status", "interrupted"),
                        event["created_at"],
                        event["data"].get("error"),
                        event["run_id"],
                    ),
                )

    def recover(self, db, sid):
        path = self.path(sid)
        row = db.execute("SELECT * FROM session_logs WHERE session_id=?", (sid,)).fetchone()
        offset = row["indexed_bytes"] if row else 0
        sequence = row["last_sequence"] if row else 0
        if not path.exists():
            if offset:
                raise Conflict("session log missing")
            return
        if path.stat().st_size < offset:
            raise Conflict("committed session log truncated")
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                raw = stream.readline(MAX_EVENT_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_EVENT_BYTES:
                    raise Conflict("session event exceeds byte budget")
                if not raw.endswith(b"\n"):
                    # 仅未提交尾部可以裁剪，原字节另存供诊断。
                    recovery = self.directory / "recovery"
                    recovery.mkdir(exist_ok=True)
                    evidence = recovery / (sid + "-" + identity() + ".tail")
                    with evidence.open("wb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    with path.open("r+b") as output:
                        output.truncate(offset)
                        output.flush()
                        os.fsync(output.fileno())
                    if row:
                        db.execute(
                            "UPDATE session_logs SET recovery_note='uncommitted_tail_recovered' "
                            "WHERE session_id=?",
                            (sid,),
                        )
                    break
                try:
                    event = json.loads(raw)
                    if (
                        event["version"] != 1
                        or event["session_id"] != sid
                        or type(event["sequence"]) is not int
                        or event["sequence"] != sequence + 1
                        or not ID_PATTERN.fullmatch(event["id"])
                        or not isinstance(event["data"], dict)
                    ):
                        raise ValueError("invalid envelope")
                except (ValueError, KeyError, TypeError) as exc:
                    raise Conflict("invalid session event") from exc
                self.ensure_log(db, event)
                self.apply(db, event)
                self.index(db, event, offset, raw)
                sequence = event["sequence"]
                offset += len(raw)

    def read(self, db, event_id):
        row = db.execute("SELECT * FROM session_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise Conflict("session event metadata missing")
        path = self.path(row["session_id"])
        if not path.exists():
            raise Conflict("session log missing")
        with path.open("rb") as stream:
            stream.seek(row["byte_offset"])
            raw = stream.read(row["byte_length"])
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise Conflict("session event hash mismatch")
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise Conflict("invalid session event") from exc
        if event.get("id") != event_id or event.get("session_id") != row["session_id"]:
            raise Conflict("session event identity mismatch")
        return event

    def hydrate(self, db, rows):
        events = {}
        result = []
        for row in rows:
            row = dict(row)
            eid = row["event_id"]
            if eid not in events:
                events[eid] = self.read(db, eid)
            event = events[eid]
            try:
                record = event["data"]["records"][row["event_index"]]
            except (KeyError, IndexError, TypeError) as exc:
                raise Conflict("entry event locator invalid") from exc
            for key in ("id", "session_id", "parent_id", "sequence", "type", "schema_version"):
                if record[key] != row[key]:
                    raise Conflict("entry metadata differs from event")
            row["data"] = json.dumps(record["data"], ensure_ascii=False)
            result.append(row)
        return result

    @staticmethod
    def project(db, entry_id, kind, data):
        valid = (
            kind == "message"
            and data.get("role") in ("user", "assistant")
            and not data.get("incomplete")
            and data.get("kind") != "tool_pair"
            and isinstance(data.get("content"), str)
        )
        for suffix in ("words", "trigrams"):
            table = "session_memory_" + suffix
            if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                db.execute(f"DELETE FROM {table} WHERE entry_id=?", (entry_id,))
                if valid:
                    db.execute(
                        f"INSERT INTO {table}(entry_id,content) VALUES(?,?)",
                        (entry_id, data["content"]),
                    )

    def put_records(self, db, event, *, replace=False):
        sid = event["session_id"]
        for index, record in enumerate(event["data"].get("records", [])):
            if (
                record["session_id"] != sid
                or record["schema_version"] != 1
                or record["type"] not in {"session_created", "message", "compaction"}
            ):
                raise Conflict("invalid entry event")
            metadata = json.dumps(self.metadata(record["data"]), ensure_ascii=False)
            exists = db.execute("SELECT id FROM entries WHERE id=?", (record["id"],)).fetchone()
            if exists and replace:
                db.execute(
                    "UPDATE entries SET data=?,event_id=?,event_index=? WHERE id=?",
                    (metadata, event["id"], index, record["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO entries(id,session_id,parent_id,sequence,type,data,schema_version,"
                    "created_at,event_id,event_index) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        record["id"],
                        sid,
                        record["parent_id"],
                        record["sequence"],
                        record["type"],
                        metadata,
                        record["schema_version"],
                        record["created_at"],
                        event["id"],
                        index,
                    ),
                )
            self.project(db, record["id"], record["type"], record["data"])

    def apply(self, db, event):
        kind, sid, data = event["type"], event["session_id"], event["data"]
        log = db.execute("SELECT * FROM session_logs WHERE session_id=?", (sid,)).fetchone()
        if log["deleted"] and kind != "session_deleted":
            if kind == "branch_deleted" and data.get("last_branch") is True:
                db.execute(
                    "UPDATE session_logs SET tombstone_event_id=? WHERE session_id=?",
                    (event["id"], sid),
                )
            return
        if kind in {"session_created", "legacy_import"}:
            session = data["session"]
            if session["id"] != sid or any(
                b["session_id"] != sid for b in data.get("branches", [])
            ):
                raise Conflict("session owner metadata mismatch")
            if not db.execute(
                "SELECT id FROM topic_materials WHERE id=?", (session["topic_material_id"],)
            ).fetchone():
                db.execute("UPDATE session_logs SET deleted=1 WHERE session_id=?", (sid,))
                return
            db.execute(
                "INSERT OR IGNORE INTO sessions VALUES(?,?,?)",
                (sid, session["topic_material_id"], session["created_at"]),
            )
            self.put_records(db, event, replace=kind == "legacy_import")
            for branch in data.get("branches", []):
                db.execute(
                    "INSERT OR IGNORE INTO branches VALUES(?,?,?,?,?,?)",
                    tuple(
                        branch[k]
                        for k in (
                            "id",
                            "session_id",
                            "title",
                            "fork_entry_id",
                            "leaf_entry_id",
                            "created_at",
                        )
                    ),
                )
        elif kind == "entries_appended":
            branch = self.store.require(db, "branches", event["branch_id"])
            if branch["session_id"] != sid or branch["leaf_entry_id"] != data["expected_leaf"]:
                raise Conflict("branch leaf changed during event recovery")
            self.put_records(db, event)
            leaf = data["records"][-1]["id"] if data["records"] else data["expected_leaf"]
            db.execute("UPDATE branches SET leaf_entry_id=? WHERE id=?", (leaf, event["branch_id"]))
        elif kind == "entry_revised":
            record = data["records"][0]
            original = self.store.require(db, "entries", record["id"])
            if any(
                original[k] != record[k]
                for k in ("id", "session_id", "parent_id", "sequence", "type", "schema_version")
            ):
                raise Conflict("revision metadata mismatch")
            self.put_records(db, event, replace=True)
        elif kind == "branch_created":
            branch = data["branch"]
            target = self.store.require(db, "entries", branch["leaf_entry_id"])
            if branch["session_id"] != sid or target["session_id"] != sid:
                raise Conflict("cross-session branch event")
            db.execute(
                "INSERT INTO branches VALUES(?,?,?,?,?,?)",
                tuple(
                    branch[k]
                    for k in (
                        "id",
                        "session_id",
                        "title",
                        "fork_entry_id",
                        "leaf_entry_id",
                        "created_at",
                    )
                ),
            )
        elif kind == "branch_deleted":
            branch = self.store.require(db, "branches", event["branch_id"])
            if branch["session_id"] != sid:
                raise Conflict("cross-session branch deletion")
            db.execute("DELETE FROM branches WHERE id=?", (event["branch_id"],))
            leaves = list(
                db.execute("SELECT leaf_entry_id FROM branches WHERE session_id=?", (sid,))
            )
            if not leaves:
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                db.execute(
                    "UPDATE session_logs SET deleted=1,tombstone_event_id=? WHERE session_id=?",
                    (event["id"], sid),
                )
            else:
                parents = {
                    r["id"]: r["parent_id"]
                    for r in db.execute(
                        "SELECT id,parent_id FROM entries WHERE session_id=?", (sid,)
                    )
                }
                reachable = set()
                for leaf in leaves:
                    current = leaf[0]
                    while current is not None and current not in reachable:
                        reachable.add(current)
                        current = parents[current]
                for eid in parents.keys() - reachable:
                    db.execute("DELETE FROM entries WHERE id=?", (eid,))
            db.execute(
                "UPDATE session_runs SET status='cancelled',ended_at=? "
                "WHERE branch_id=? AND status='running'",
                (event["created_at"], event["branch_id"]),
            )
        elif kind == "session_deleted":
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            db.execute(
                "UPDATE session_logs SET deleted=1,tombstone_event_id=? WHERE session_id=?",
                (event["id"], sid),
            )
        elif kind.startswith("agent."):
            if kind not in {"agent.run_finished", "agent.run_interrupted"}:
                branch = self.store.require(db, "branches", event["branch_id"])
                if branch["session_id"] != sid:
                    raise Conflict("runtime event outside session")
        else:
            raise Conflict("unsupported session event")

    def migrate(self, db, session):
        sid = session["id"]
        self.recover(db, sid)
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM entries WHERE session_id=? AND event_id IS NULL ORDER BY sequence",
                (sid,),
            )
        ]
        branches = [
            dict(r) for r in db.execute("SELECT * FROM branches WHERE session_id=?", (sid,))
        ]
        for start in range(0, len(rows), 32):
            records = []
            for row in rows[start : start + 32]:
                row["data"] = json.loads(row["data"])
                row.pop("event_id", None)
                row.pop("event_index", None)
                records.append(row)
            event = self.event(
                db,
                sid,
                "legacy_import",
                {"session": session, "branches": branches, "records": records},
            )
            self.persist(db, event)

    def flush_deleted(self, db):
        for row in list(
            db.execute("SELECT * FROM session_logs WHERE deleted=1 AND tombstone_event_id IS NULL")
        ):
            sid = row["session_id"]
            self.recover(db, sid)
            event = self.event(db, sid, "session_deleted", {"reason": "owner_or_session_deleted"})
            self.persist(db, event)
