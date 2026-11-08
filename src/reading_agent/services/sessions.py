"""共享 Entry 树、事务性叶推进、路径回放与只剪独占尾部。"""

import json

from reading_agent.agents.models import HistoryMessage
from reading_agent.storage.database import Conflict, identity, now

from .session_events import SessionEvents

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,topic_material_id TEXT NOT NULL REFERENCES topic_materials(id) ON DELETE
        CASCADE,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 parent_id TEXT REFERENCES entries(id) DEFERRABLE INITIALLY DEFERRED,
 sequence INTEGER NOT NULL,type TEXT NOT NULL,data TEXT NOT NULL,schema_version INTEGER NOT
        NULL DEFAULT 1,
 created_at TEXT NOT NULL,UNIQUE(session_id,sequence));
CREATE TABLE IF NOT EXISTS branches(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 title TEXT NOT NULL,fork_entry_id TEXT REFERENCES entries(id) DEFERRABLE INITIALLY DEFERRED,
 leaf_entry_id TEXT NOT NULL REFERENCES entries(id) DEFERRABLE INITIALLY DEFERRED,
 created_at TEXT NOT NULL);
"""


class Sessions:
    def __init__(self, store, *, directory=None, initialize=True):
        self.store = store
        self.journal = SessionEvents(store, directory)
        if initialize:
            with self.journal.lock():
                with store.transaction() as db:
                    db.executescript(SCHEMA)
                    self.journal.initialize(db)

    @staticmethod
    def _record(sid, eid, parent, sequence, kind, data, created=None):
        return {
            "id": eid,
            "session_id": sid,
            "parent_id": parent,
            "sequence": sequence,
            "type": kind,
            "data": data,
            "schema_version": 1,
            "created_at": created or now(),
        }

    def create(self, tm_id):
        sid, root, bid = identity(), identity(), identity()
        with self.journal.lock():
            with self.store.transaction() as db:
                tm = self.store.require(db, "topic_materials", tm_id)
                material = self.store.require(db, "materials", tm["material_id"])
                snapshot = {
                    "title": material["title"],
                    "material_id": tm["material_id"],
                    "topic_id": tm["topic_id"],
                    "summary": tm["deep_summary"] or tm["short_summary"],
                }
                created = now()
                session = {"id": sid, "topic_material_id": tm_id, "created_at": created}
                branch = {
                    "id": bid,
                    "session_id": sid,
                    "title": "主讨论",
                    "fork_entry_id": None,
                    "leaf_entry_id": root,
                    "created_at": created,
                }
                event = self.journal.event(
                    db,
                    sid,
                    "session_created",
                    {
                        "session": session,
                        "branches": [branch],
                        "records": [
                            self._record(sid, root, None, 0, "session_created", snapshot, created)
                        ],
                    },
                    branch_id=bid,
                )
                self.journal.persist(db, event)
        return {"session_id": sid, "branch_id": bid}

    def all_entries(self, session_id):
        with self.journal.lock():
            with self.store.transaction() as db:
                self.journal.recover(db, session_id)
                self.store.require(db, "sessions", session_id)
                rows = list(
                    db.execute(
                        "SELECT * FROM entries WHERE session_id=? ORDER BY sequence", (session_id,)
                    )
                )
                return self.journal.hydrate(db, rows)

    def read_entry(self, entry_id):
        with self.journal.lock():
            with self.store.transaction() as db:
                row = self.store.require(db, "entries", entry_id)
                self.journal.recover(db, row["session_id"])
                row = self.store.require(db, "entries", entry_id)
                return self.journal.hydrate(db, [row])[0]

    def path(self, branch_id):
        with self.journal.lock():
            with self.store.transaction() as db:
                branch = self.store.require(db, "branches", branch_id)
                sid = branch["session_id"]
                self.journal.recover(db, sid)
                branch = self.store.require(db, "branches", branch_id)
                rows = self.journal.hydrate(
                    db, list(db.execute("SELECT * FROM entries WHERE session_id=?", (sid,)))
                )
        entries = {r["id"]: r for r in rows}
        path, current, seen = [], branch["leaf_entry_id"], set()
        while current is not None:
            if current in seen or current not in entries:
                raise Conflict("invalid history ancestry")
            seen.add(current)
            entry = entries[current]
            if entry["schema_version"] != 1 or entry["type"] not in {
                "session_created",
                "message",
                "compaction",
            }:
                raise Conflict("unsupported Entry type/version")
            entry["data"] = json.loads(entry["data"])
            path.append(entry)
            current = entry["parent_id"]
        path.reverse()
        if (
            not path
            or path[0]["type"] != "session_created"
            or sum(e["type"] == "session_created" for e in path) != 1
        ):
            raise Conflict("missing or multiple stable roots")
        return path

    def append(self, branch_id, expected_leaf, records, *, run_id=None):
        with self.journal.lock():
            with self.store.transaction() as db:
                branch = self.store.require(db, "branches", branch_id)
                sid = branch["session_id"]
                self.journal.recover(db, sid)
                branch = self.store.require(db, "branches", branch_id)
                if branch["leaf_entry_id"] != expected_leaf:
                    raise Conflict("branch leaf changed")
                sequence = db.execute(
                    "SELECT MAX(sequence) FROM entries WHERE session_id=?", (sid,)
                ).fetchone()[0]
                parent = expected_leaf
                result = []
                for kind, data in records:
                    if kind not in {"message", "compaction"} or not isinstance(data, dict):
                        raise ValueError("invalid entry type or payload")
                    sequence += 1
                    eid = identity()
                    result.append(self._record(sid, eid, parent, sequence, kind, data))
                    parent = eid
                event = self.journal.event(
                    db,
                    sid,
                    "entries_appended",
                    {"expected_leaf": expected_leaf, "records": result},
                    branch_id=branch_id,
                    run_id=run_id,
                )
                self.journal.persist(db, event)
        return parent

    def revise(self, entry_id, data):
        """追加修订事件，保留同一 Entry 身份；禁止通过 SQL 缓存改写原始正文。"""
        with self.journal.lock():
            with self.store.transaction() as db:
                entry = dict(self.store.require(db, "entries", entry_id))
                sid = entry["session_id"]
                self.journal.recover(db, sid)
                entry = dict(self.store.require(db, "entries", entry_id))
                if entry["type"] != "message" or not isinstance(data, dict):
                    raise ValueError("only message payload may be revised")
                record = self._record(
                    sid,
                    entry_id,
                    entry["parent_id"],
                    entry["sequence"],
                    entry["type"],
                    data,
                    entry["created_at"],
                )
                event = self.journal.event(db, sid, "entry_revised", {"records": [record]})
                self.journal.persist(db, event)

    def prepare_model_input(self, entry_id, question, model_input, *, branch_id):
        """保存当轮真正发送的用户输入；历史回放不重新召回或刷新旧时间。"""
        with self.journal.lock():
            with self.store.transaction() as db:
                entry = self.store.require(db, "entries", entry_id)
                self.journal.recover(db, entry["session_id"])
                entry = self.journal.hydrate(db, [self.store.require(db, "entries", entry_id)])[0]
                data = json.loads(entry["data"])
                branch = self.store.require(db, "branches", branch_id)
                if (
                    branch["session_id"] != entry["session_id"]
                    or branch["leaf_entry_id"] != entry_id
                ):
                    raise Conflict("prompt input is not current branch leaf")
                if (
                    entry["type"] != "message"
                    or data.get("role") != "user"
                    or data.get("content") != question
                ):
                    raise Conflict("prompt input source changed")
                if "model_input" in data:
                    if not isinstance(data["model_input"], str):
                        raise Conflict("invalid saved prompt input")
                    return data["model_input"]
                data["model_input"] = model_input
                record = self._record(
                    entry["session_id"],
                    entry_id,
                    entry["parent_id"],
                    entry["sequence"],
                    entry["type"],
                    data,
                    entry["created_at"],
                )
                self.journal.persist(
                    db,
                    self.journal.event(
                        db, entry["session_id"], "entry_revised", {"records": [record]}
                    ),
                )
                return model_input

    def fork(self, branch_id, entry_id, title="分支讨论"):
        path = self.path(branch_id)
        if entry_id not in {e["id"] for e in path}:
            raise ValueError("fork point not on selected path")
        bid = identity()
        with self.journal.lock():
            with self.store.transaction() as db:
                source = self.store.require(db, "branches", branch_id)
                sid = source["session_id"]
                self.journal.recover(db, sid)
                entry = self.store.require(db, "entries", entry_id)
                if entry["session_id"] != sid:
                    raise Conflict("cross-session fork")
                # 在同一事务再次验证目标仍位于当前分支路径。
                current = self.store.require(db, "branches", branch_id)["leaf_entry_id"]
                visited = set()
                while current is not None and current not in visited and current != entry_id:
                    visited.add(current)
                    current = self.store.require(db, "entries", current)["parent_id"]
                if current != entry_id:
                    raise Conflict("fork point moved off path")
                branch = {
                    "id": bid,
                    "session_id": sid,
                    "title": title,
                    "fork_entry_id": entry_id,
                    "leaf_entry_id": entry_id,
                    "created_at": now(),
                }
                self.journal.persist(
                    db,
                    self.journal.event(
                        db, sid, "branch_created", {"branch": branch}, branch_id=bid
                    ),
                )
        return bid

    def delete_branch(self, branch_id):
        with self.journal.lock():
            with self.store.transaction() as db:
                sid = self.store.require(db, "branches", branch_id)["session_id"]
                self.journal.recover(db, sid)
                self.journal.persist(
                    db,
                    self.journal.event(
                        db,
                        sid,
                        "branch_deleted",
                        {
                            "last_branch": db.execute(
                                "SELECT count(*) FROM branches WHERE session_id=?", (sid,)
                            ).fetchone()[0]
                            == 1
                        },
                        branch_id=branch_id,
                    ),
                )

    def record_event(self, branch_id, run_id, kind, data):
        if not isinstance(data, dict) or not kind or len(kind) > 80:
            raise ValueError("invalid runtime event")
        with self.journal.lock():
            with self.store.transaction() as db:
                sid = self.store.require(db, "branches", branch_id)["session_id"]
                self.journal.recover(db, sid)
                self.journal.persist(
                    db,
                    self.journal.event(
                        db, sid, "agent." + kind, data, branch_id=branch_id, run_id=run_id
                    ),
                )

    def events(self, sid, *, after=0, limit=100, run_id=None):
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("invalid event page")
        with self.journal.lock():
            with self.store.transaction() as db:
                self.journal.recover(db, sid)
                self.store.require(db, "sessions", sid)
                sql = "SELECT id FROM session_events WHERE session_id=? AND sequence>?"
                params = [sid, after]
                if run_id is not None:
                    sql += " AND run_id=?"
                    params.append(run_id)
                rows = list(db.execute(sql + " ORDER BY sequence LIMIT ?", params + [limit]))
                return [self.journal.read(db, r["id"]) for r in rows]

    def flush_deleted(self):
        with self.journal.lock():
            with self.store.transaction() as db:
                self.journal.flush_deleted(db)

    def interrupt_runs(self):
        rows = self.store.all("SELECT * FROM session_runs WHERE status='running'")
        for row in rows:
            if self.store.all("SELECT id FROM branches WHERE id=?", (row["branch_id"],)):
                self.record_event(
                    row["branch_id"],
                    row["id"],
                    "run_interrupted",
                    {"status": "interrupted", "error": "process_restarted"},
                )

    def recovery_state(self, branch_id):
        from reading_agent.agents.recovery import reconcile, validate_checkpoint

        with self.journal.lock(), self.store.transaction() as db:
            branch = self.store.require(db, "branches", branch_id)
            self.journal.recover(db, branch["session_id"])
            run = db.execute(
                "SELECT * FROM session_runs WHERE branch_id=? ORDER BY last_sequence DESC LIMIT 1",
                (branch_id,),
            ).fetchone()
            if not run or run["status"] != "interrupted":
                return {"can_resume": False, "reason": "only_interrupted_runs_can_resume"}

            def event(kind, latest=True):
                row = db.execute(
                    "SELECT id,sequence FROM session_events "
                    "WHERE run_id=? AND type=? ORDER BY sequence "
                    + ("DESC" if latest else "ASC")
                    + " LIMIT 1",
                    (run["id"], "agent." + kind),
                ).fetchone()
                return (self.journal.read(db, row["id"]), row["sequence"]) if row else (None, 0)

            started, _ = event("run_started", False)
            saved, sequence = event("checkpoint")
            if not started:
                return {"can_resume": False, "reason": "run_start_missing"}
            expected = started["data"].get("question_entry_id")
            if branch["leaf_entry_id"] != expected:
                return {"can_resume": False, "reason": "branch_changed_or_result_already_saved"}
            state = saved["data"]["state"] if saved else started["data"].get("recovery")
            if not state:
                return {"can_resume": False, "reason": "checkpoint_missing"}
            if not validate_checkpoint(state):
                return {"can_resume": False, "reason": "checkpoint_invalid"}
            tail = [
                self.journal.read(db, row[0])
                for row in db.execute(
                    "SELECT id FROM session_events WHERE run_id=? AND sequence>? "
                    "AND type IN ('agent.tool_requested','agent.tool_result') ORDER BY sequence",
                    (run["id"], sequence),
                )
            ]
            state = reconcile(state, tail)
            pulse, _ = event("recovery_heartbeat")
            if pulse:
                state["report"]["elapsed_seconds"] = max(
                    state["report"]["elapsed_seconds"], pulse["data"]["elapsed_seconds"]
                )
            state["report"]["elapsed_seconds"] += (
                1  # 最后心跳间隔内未落盘用时的保守补偿，不计宕机时间。
            )
            state["report"]["usage_complete"] = False
            entry = self.store.require(db, "entries", expected)
            entry = self.journal.hydrate(db, [entry])[0]
            data = json.loads(entry["data"])
            if data.get("role") != "user" or data.get("content") != state["task"]["question"]:
                return {"can_resume": False, "reason": "question_changed"}
            return {
                "can_resume": True,
                "run_id": run["id"],
                "question_entry_id": expected,
                "question": data["content"],
                "state": state,
                "unknown_operations": [
                    {"name": value["name"], "arguments": value["arguments"]}
                    for value in state.get("outcomes", {}).values()
                    if value.get("unknown")
                ],
            }

    def last_run(self, branch_id):
        """从JSONL恢复最后一次运行的数值快照，SQL仅用于定位和最终状态。"""
        with self.journal.lock():
            with self.store.transaction() as db:
                branch = self.store.require(db, "branches", branch_id)
                self.journal.recover(db, branch["session_id"])
                run = db.execute(
                    "SELECT * FROM session_runs WHERE branch_id=? ORDER BY started_at DESC LIMIT 1",
                    (branch_id,),
                ).fetchone()
                if run is None:
                    return None
                event = db.execute(
                    "SELECT id,type FROM session_events WHERE session_id=? AND run_id=? "
                    "AND type IN ('agent.telemetry','agent.run_finished') "
                    "ORDER BY sequence DESC LIMIT 1",
                    (branch["session_id"], run["id"]),
                ).fetchone()
                metrics = {}
                if event:
                    data = self.journal.read(db, event["id"])["data"]
                    metrics = (
                        data.get("metrics", {}) if event["type"] == "agent.run_finished" else data
                    )
                return {
                    "run_id": run["id"],
                    "phase": run["status"],
                    "error": run["error"],
                    "metrics": {**metrics, "phase": run["status"]},
                }

    def rebuild(self, sid):
        """重建指定日志的结构/事件元数据；保留 SQL 中已有删除状态。"""
        with self.journal.lock():
            with self.store.transaction() as db:
                log = dict(
                    db.execute("SELECT * FROM session_logs WHERE session_id=?", (sid,)).fetchone()
                    or {}
                )
                if not log:
                    raise Conflict("unknown session log")
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                db.execute("DELETE FROM session_events WHERE session_id=?", (sid,))
                db.execute("DELETE FROM session_runs WHERE session_id=?", (sid,))
                db.execute(
                    "UPDATE session_logs SET indexed_bytes=0,last_sequence=0,deleted=?,"
                    "tombstone_event_id=NULL WHERE session_id=?",
                    (log["deleted"], sid),
                )
                self.journal.recover(db, sid)
                self.journal.flush_deleted(db)

    def rebuild_search_index(self):
        with self.journal.lock():
            with self.store.transaction() as db:
                for suffix, tokenizer in (("words", "unicode61"), ("trigrams", "trigram")):
                    table = "session_memory_" + suffix
                    db.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                        f"USING fts5(entry_id UNINDEXED,content,tokenize='{tokenizer}')"
                    )
                    for operation in ("insert", "update"):
                        db.execute(f"DROP TRIGGER IF EXISTS memory_{suffix}_{operation}")
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS memory_{suffix}_delete "
                        "AFTER DELETE ON entries "
                        f"BEGIN DELETE FROM {table} WHERE entry_id=OLD.id; END;"
                    )
                    db.execute(f"DELETE FROM {table}")
                count = 0
                for session in list(db.execute("SELECT id FROM sessions")):
                    sid = session["id"]
                    self.journal.recover(db, sid)
                    rows = self.journal.hydrate(
                        db, list(db.execute("SELECT * FROM entries WHERE session_id=?", (sid,)))
                    )
                    for row in rows:
                        data = json.loads(row["data"])
                        self.journal.project(db, row["id"], row["type"], data)
                        if (
                            row["type"] == "message"
                            and data.get("role") in ("user", "assistant")
                            and not data.get("incomplete")
                            and data.get("kind") != "tool_pair"
                        ):
                            count += 1
                return count

    def context(self, branch_id, *, exclude_ids=()):
        path = self.path(branch_id)
        covered = set()
        for entry in path:
            if entry["type"] == "compaction":
                covers = entry["data"]["covers"]
                ancestors = {e["id"] for e in path if e["sequence"] < entry["sequence"]}
                if not set(covers) <= ancestors:
                    raise Conflict("compaction outside current path")
                covered.update(covers)
        history = []
        for entry in path[1:]:
            if entry["id"] in covered or entry["id"] in exclude_ids:
                continue
            data = entry["data"]
            if entry["type"] == "compaction":
                history.append(
                    HistoryMessage(
                        role="assistant", content="此前讨论压缩（非长期记忆）：" + data["summary"]
                    )
                )
            elif entry["type"] == "message":
                if data.get("kind") == "tool_pair":
                    # 完整请求/结果配对作为事实块传入；不把旧调用再次当作待执行指令。
                    history.append(
                        HistoryMessage(
                            role="assistant",
                            content="历史工具请求及实际结果：" + self.store.dump_json(data),
                        )
                    )
                else:
                    text = (
                        data.get("model_input", data.get("content", ""))
                        if data.get("role") == "user"
                        else data.get("content", "")
                    )
                    if data.get("incomplete"):
                        text += "\n[回复未完成，不代表任务成功]"
                    history.append(HistoryMessage(role=data["role"], content=text))
        return self.store.dump_json(path[0]["data"]), history
