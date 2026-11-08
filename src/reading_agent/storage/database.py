"""SQLite 权威存储。每次操作使用短事务，网络和模型不进入事务。"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def identity():
    return uuid4().hex


def now():
    return datetime.now(UTC).isoformat()


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT
        NOT NULL);
CREATE TABLE IF NOT EXISTS tags(
 id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
 name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', UNIQUE(id,topic_id));
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, source_key TEXT NOT NULL, name TEXT NOT NULL, entry_url TEXT NOT NULL,
 is_enabled INTEGER NOT NULL DEFAULT 1 CHECK(is_enabled IN (0,1)),
 is_removed INTEGER NOT NULL DEFAULT 0 CHECK(is_removed IN (0,1)),
 created_at TEXT NOT NULL,last_fetched_at TEXT,
 CHECK(is_removed=0 OR is_enabled=0));
CREATE UNIQUE INDEX IF NOT EXISTS source_identity ON sources(source_key) WHERE is_removed=0;
CREATE TABLE IF NOT EXISTS topic_sources(
 id TEXT PRIMARY KEY,topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id),is_linked INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(topic_id,source_id));
CREATE TABLE IF NOT EXISTS materials(
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),item_key TEXT NOT NULL,
 url TEXT NOT NULL,title TEXT NOT NULL,source_summary TEXT NOT NULL DEFAULT '',published_at TEXT,
 created_at TEXT NOT NULL,routing_status TEXT NOT NULL DEFAULT 'pending'
 CHECK(routing_status IN ('pending','running','succeeded','failed')),
 routing_error TEXT,routed_at TEXT,UNIQUE(source_id,item_key));
CREATE TABLE IF NOT EXISTS material_bodies(
 material_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
 content TEXT NOT NULL,fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_materials(
 id TEXT PRIMARY KEY,topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
 material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
 inbox_at TEXT NOT NULL,topic_match_reason TEXT,
 short_summary TEXT NOT NULL CHECK(length(trim(short_summary))>0),
 is_recommended INTEGER NOT NULL CHECK(is_recommended IN (0,1)),recommendation_reason TEXT NOT NULL,
 needs_deep_read INTEGER NOT NULL CHECK(needs_deep_read IN (0,1)),deep_read_guideline TEXT NOT NULL,
 deep_summary TEXT,deep_summary_status TEXT NOT NULL DEFAULT 'not_scheduled'
 CHECK(deep_summary_status IN (
 'not_scheduled','pending','running','succeeded','failed','cancelled')),
 deep_summary_error TEXT,deep_summarized_at TEXT,is_read INTEGER NOT NULL DEFAULT 0,
 interest TEXT NOT NULL DEFAULT 'none' CHECK(interest IN ('none','interested','uninterested')),
 UNIQUE(topic_id,material_id),UNIQUE(id,topic_id),
 CHECK(needs_deep_read=0 OR length(trim(deep_read_guideline))>0));
CREATE TABLE IF NOT EXISTS material_tags(
 topic_material_id TEXT NOT NULL,tag_id TEXT NOT NULL,topic_id TEXT NOT NULL,
 FOREIGN KEY(topic_material_id,topic_id) REFERENCES topic_materials(id,topic_id) ON DELETE CASCADE,
 FOREIGN KEY(tag_id,topic_id) REFERENCES tags(id,topic_id) ON DELETE CASCADE,
 PRIMARY KEY(topic_material_id,tag_id));
CREATE INDEX IF NOT EXISTS inbox_topic ON topic_materials(topic_id,inbox_at);
"""


class Store:
    def __init__(self, path):
        self.change_listeners = set()
        self.revision = 0
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        changed = set()

        def authorize(action, table, *args):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                if table in {
                    "topics",
                    "tags",
                    "sources",
                    "topic_sources",
                    "materials",
                    "topic_materials",
                    "material_tags",
                }:
                    changed.add(table)
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        committed = False
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
            committed = bool(db.total_changes and changed)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        if committed:
            self.publish_change()

    def publish_change(self):
        self.revision += 1
        for listener in tuple(self.change_listeners):
            listener(self.revision)

    def all(self, sql, parameters=()):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def one(self, sql, parameters=()):
        rows = self.all(sql, parameters)
        if not rows:
            raise NotFound("record not found")
        return rows[0]

    def require(self, db, table, item_id):
        if table not in {
            "topics",
            "tags",
            "sources",
            "materials",
            "topic_materials",
            "branches",
            "sessions",
            "entries",
            "memories",
        }:
            raise ValueError("invalid table")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("record not found")
        return dict(row)

    def topic(self, name, description=""):
        if not name.strip() or len(name) > 200 or len(description) > 4000:
            raise ValueError("invalid Topic name/description")
        tid = identity()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO topics VALUES(?,?,?,?)", (tid, name.strip(), description, now())
            )
        return tid

    def tag(self, topic_id, name, description=""):
        if not name.strip() or len(name) > 200 or len(description) > 1000:
            raise ValueError("invalid tag")
        tag_id = identity()
        with self.transaction() as db:
            self.require(db, "topics", topic_id)
            db.execute(
                "INSERT INTO tags VALUES(?,?,?,?)", (tag_id, topic_id, name.strip(), description)
            )
        return tag_id

    def link(self, topic_id, source_id, linked=True):
        with self.transaction() as db:
            self.require(db, "topics", topic_id)
            source = self.require(db, "sources", source_id)
            if source["is_removed"]:
                raise Conflict("source removed")
            db.execute(
                """INSERT INTO topic_sources VALUES(?,?,?,?,?,?)
              ON CONFLICT(topic_id,source_id) DO UPDATE SET
              is_linked=excluded.is_linked,updated_at=excluded.updated_at""",
                (identity(), topic_id, source_id, int(linked), now(), now()),
            )

    def materials(self):
        return self.all("""SELECT m.*,COUNT(tm.id) reference_count FROM materials m
            LEFT JOIN topic_materials tm ON tm.material_id=m.id GROUP BY m.id ORDER BY
        m.created_at DESC""")

    def inbox(self, topic_id, *, recommended=False, tags=(), untagged=False):
        rows = self.all(
            """SELECT tm.*,m.title,m.url,m.created_at,m.published_at
          FROM topic_materials tm JOIN materials m ON m.id=tm.material_id
          WHERE tm.topic_id=? ORDER BY m.created_at DESC,tm.id""",
            (topic_id,),
        )
        result = []
        for row in rows:
            row["tag_ids"] = [
                t["tag_id"]
                for t in self.all(
                    "SELECT tag_id FROM material_tags WHERE topic_material_id=?", (row["id"],)
                )
            ]
            row["recommended"] = bool(
                row["is_recommended"] and row["tag_ids"] and row["interest"] != "uninterested"
            )
            if recommended and not row["recommended"]:
                continue
            if not set(tags) <= set(row["tag_ids"]) or (untagged and row["tag_ids"]):
                continue
            result.append(row)
        return result

    def feedback(self, tm_id, *, is_read=None, interest=None, tag_ids=None):
        if interest is not None and interest not in {"none", "interested", "uninterested"}:
            raise ValueError("invalid interest")
        with self.transaction() as db:
            row = self.require(db, "topic_materials", tm_id)
            if is_read is not None:
                db.execute("UPDATE topic_materials SET is_read=? WHERE id=?", (int(is_read), tm_id))
            if interest is not None:
                db.execute("UPDATE topic_materials SET interest=? WHERE id=?", (interest, tm_id))
                if interest == "uninterested" and row["deep_summary_status"] == "pending":
                    db.execute(
                        "UPDATE topic_materials SET deep_summary_status='cancelled' WHERE id=?",
                        (tm_id,),
                    )
            if tag_ids is not None:
                legal = {
                    t[0]
                    for t in db.execute("SELECT id FROM tags WHERE topic_id=?", (row["topic_id"],))
                }
                if not set(tag_ids) <= legal:
                    raise ValueError("tag outside Topic")
                db.execute("DELETE FROM material_tags WHERE topic_material_id=?", (tm_id,))
                for tag_id in set(tag_ids):
                    db.execute(
                        "INSERT INTO material_tags VALUES(?,?,?)", (tm_id, tag_id, row["topic_id"])
                    )

    def delete(self, table, item_id):
        if table not in {"topics", "tags", "topic_materials", "materials", "sessions"}:
            raise ValueError("unsupported deletion")
        with self.transaction() as db:
            self.require(db, table, item_id)
            if (
                table == "materials"
                and db.execute(
                    "SELECT 1 FROM topic_materials WHERE material_id=?", (item_id,)
                ).fetchone()
            ):
                raise Conflict("material still referenced")
            affected = []
            if table == "tags":
                affected = [
                    r[0]
                    for r in db.execute(
                        "SELECT topic_material_id FROM material_tags WHERE tag_id=?", (item_id,)
                    )
                ]
            db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            for tm_id in affected:
                db.execute(
                    """UPDATE topic_materials SET deep_summary_status='cancelled'
                   WHERE id=? AND deep_summary_status='pending' AND NOT EXISTS(
                   SELECT 1 FROM material_tags WHERE topic_material_id=?)""",
                    (tm_id, tm_id),
                )

    def interrupt(self):
        with self.transaction() as db:
            db.execute("""UPDATE materials SET routing_status='failed',routing_error='上次运行中断'
                       WHERE routing_status IN ('pending','running')""")
            db.execute("""UPDATE topic_materials SET deep_summary_status='failed',
                       deep_summary_error='上次运行中断' WHERE deep_summary_status IN
        ('pending','running')""")

    def dump_json(self, value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
