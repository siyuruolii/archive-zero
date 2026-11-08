"""JSONL 权威事件、SQL 元数据、运行流与恢复边界。"""

import hashlib
import json

import httpx
import pytest
from conftest import ScriptedProvider, route_json
from desktop_fixture import seed
from test_materials import item, prepare
from test_sessions_memory import append

from reading_agent.agents.models import RouteFinal
from reading_agent.agents.provider import ToolCall, Turn
from reading_agent.infrastructure.locking import FileLock
from reading_agent.interfaces.api import create_app
from reading_agent.services.discussion import Discussion
from reading_agent.services.session_events import SessionEvents
from reading_agent.services.sessions import SCHEMA, Sessions
from reading_agent.storage.database import Conflict, NotFound, identity, now


def lines(sessions, sid):
    return [json.loads(line) for line in sessions.journal.path(sid).read_bytes().splitlines()]


async def legacy(setup):
    service, _, mid, tid, tag = await prepare(setup)
    store = service.store
    tm = service.commit_route(mid, RouteFinal.model_validate_json(route_json([item(tid, tag)])))[0]
    sid, root, eid, bid = (identity() for _ in range(4))
    created = now()
    with store.transaction() as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (sid, tm, created))
        db.execute(
            "INSERT INTO entries VALUES(?,?,NULL,0,'session_created',?,1,?)",
            (
                root,
                sid,
                json.dumps(
                    {"title": "旧根", "material_id": mid, "topic_id": tid, "summary": "旧摘要"}
                ),
                created,
            ),
        )
        db.execute(
            "INSERT INTO entries VALUES(?,?,?,1,'message',?,1,?)",
            (
                eid,
                sid,
                root,
                json.dumps({"role": "user", "content": "旧中文消息"}, ensure_ascii=False),
                created,
            ),
        )
        db.execute(
            "INSERT INTO branches VALUES(?,?,?,NULL,?,?)", (bid, sid, "旧分支", eid, created)
        )
    return store, sid, bid, eid


async def test_entry_payload_is_jsonl_and_sqlite_contains_only_locator_and_message_metadata(state):
    store, _, sessions, _, created, _, _ = state
    bid, sid = created["branch_id"], created["session_id"]
    eid = append(sessions, bid, "中文正文\n第二行🙂")
    row = store.one("SELECT * FROM entries WHERE id=?", (eid,))
    assert json.loads(row["data"]) == {"role": "user"}
    assert row["event_id"] and row["event_index"] == 0
    assert sessions.path(bid)[-1]["data"]["content"] == "中文正文\n第二行🙂"
    events = lines(sessions, sid)
    assert events[-1]["data"]["records"][0]["data"]["content"] == "中文正文\n第二行🙂"
    metadata = store.all(
        "SELECT * FROM session_events WHERE session_id=? ORDER BY sequence", (sid,)
    )
    raw = sessions.journal.path(sid).read_bytes()
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    for ref, event in zip(metadata, events, strict=True):
        data = raw[ref["byte_offset"] : ref["byte_offset"] + ref["byte_length"]]
        assert json.loads(data) == event and hashlib.sha256(data).hexdigest() == ref["sha256"]
        assert "data" not in ref and "content" not in ref


async def test_sql_payload_cache_cannot_override_authoritative_event(state):
    store, _, sessions, _, created, _, _ = state
    eid = append(sessions, created["branch_id"], "实际正文")
    with store.transaction() as db:
        db.execute(
            "UPDATE entries SET data=? WHERE id=?", (json.dumps({"content": "伪造缓存"}), eid)
        )
    assert sessions.path(created["branch_id"])[-1]["data"]["content"] == "实际正文"


async def test_legacy_migration_preserves_branch_ids_and_clears_sql_body(setup):
    store, sid, bid, eid = await legacy(setup)
    sessions = Sessions(store)
    assert sessions.path(bid)[-1]["id"] == eid
    assert sessions.path(bid)[-1]["data"]["content"] == "旧中文消息"
    assert json.loads(store.one("SELECT data FROM entries WHERE id=?", (eid,))["data"]) == {
        "role": "user"
    }
    assert [e["type"] for e in lines(sessions, sid)] == ["legacy_import"]
    Sessions(store)
    assert len(lines(sessions, sid)) == 1


async def test_legacy_migration_recovers_after_file_fsync_but_sql_failure(setup, monkeypatch):
    store, sid, bid, eid = await legacy(setup)
    original = SessionEvents.index

    def fail(self, db, event, offset, raw):
        if event["type"] == "legacy_import":
            raise OSError("simulated metadata failure")
        return original(self, db, event, offset, raw)

    with monkeypatch.context() as patch:
        patch.setattr(SessionEvents, "index", fail)
        with pytest.raises(OSError):
            Sessions(store)
    assert "content" in json.loads(store.one("SELECT data FROM entries WHERE id=?", (eid,))["data"])
    sessions = Sessions(store)
    assert sessions.path(bid)[-1]["data"]["content"] == "旧中文消息"
    assert len(lines(sessions, sid)) == 1


async def test_legacy_migration_splits_batches_and_keeps_every_locator_correct(setup):
    store, sid, bid, eid = await legacy(setup)
    parent = eid
    with store.transaction() as db:
        for sequence in range(2, 36):
            entry_id = identity()
            db.execute(
                "INSERT INTO entries VALUES(?,?,?,?,'message',?,1,?)",
                (
                    entry_id,
                    sid,
                    parent,
                    sequence,
                    json.dumps({"role": "user", "content": "legacy-" + str(sequence)}),
                    now(),
                ),
            )
            parent = entry_id
        db.execute("UPDATE branches SET leaf_entry_id=? WHERE id=?", (parent, bid))
    sessions = Sessions(store)
    assert len(lines(sessions, sid)) == 2
    assert sessions.path(bid)[-1]["data"]["content"] == "legacy-35"
    assert len(sessions.all_entries(sid)) == 36
    sessions.rebuild(sid)
    assert sessions.path(bid)[-1]["id"] == parent


@pytest.mark.parametrize("delete_owner", [False, True])
async def test_unacknowledged_session_creation_recovers_without_reviving_deleted_owner(
    state, monkeypatch, delete_owner
):
    store, _, sessions, _, created, _, tm = state
    old_sid = created["session_id"]

    def fail(*args):
        raise OSError("simulated metadata failure")

    with monkeypatch.context() as patch:
        patch.setattr(sessions.journal, "index", fail)
        with pytest.raises(OSError):
            sessions.create(tm)
    orphan = next(p for p in sessions.journal.directory.glob("*.jsonl") if p.stem != old_sid)
    first = json.loads(orphan.read_bytes().splitlines()[0])
    bid = first["data"]["branches"][0]["id"]
    if delete_owner:
        store.delete("topic_materials", tm)
    recovered = Sessions(store)
    if delete_owner:
        assert store.all("SELECT * FROM sessions") == []
        assert lines(recovered, orphan.stem)[-1]["type"] == "session_deleted"
    else:
        assert recovered.path(bid)[0]["type"] == "session_created"
        assert len(lines(recovered, orphan.stem)) == 1


async def test_memory_extraction_rechecks_jsonl_source_after_model_await(state):
    _, _, sessions, memory, created, tid, _ = state
    bid = created["branch_id"]
    eid = append(sessions, bid, "我熟悉Java")
    append(sessions, bid, "收到", "assistant")

    class ChangingSource(ScriptedProvider):
        async def stream(self, request):
            sessions.revise(eid, {"role": "user", "content": "我没有学过Java"})
            async for event in super().stream(request):
                yield event

    memory.extraction_provider = ChangingSource(
        '{"profiles":[{"content":"用户熟悉Java","quote":"我熟悉Java","explicit_global":false}]}'
    )
    await memory.extract(bid, "我熟悉Java")
    assert memory.last_extraction_error == "Conflict"
    assert memory.list(tid, category="profile") == []


async def test_durable_unindexed_append_recovers_once_without_replaying_model_or_tools(
    state, monkeypatch
):
    store, service, sessions, memory, created, tid, _ = state
    bid, sid = created["branch_id"], created["session_id"]
    leaf = sessions.path(bid)[-1]["id"]
    original = sessions.journal.index

    def fail(db, event, offset, raw):
        raise OSError("simulated metadata failure")

    with monkeypatch.context() as patch:
        patch.setattr(sessions.journal, "index", fail)
        with pytest.raises(OSError):
            sessions.append(bid, leaf, [("message", {"role": "user", "content": "可恢复的消息"})])
    assert len(store.all("SELECT * FROM entries WHERE session_id=?", (sid,))) == 1
    assert sessions.path(bid)[-1]["data"]["content"] == "可恢复的消息"
    assert len(store.all("SELECT * FROM entries WHERE session_id=?", (sid,))) == 2
    assert len(sessions.events(sid)) == 2 and len(sessions.events(sid)) == 2
    assert memory.search_sessions("可恢复") and service.provider.requests == []
    assert original is not None


async def test_incomplete_uncommitted_tail_is_quarantined_but_committed_bytes_stay_unchanged(state):
    store, _, sessions, _, created, _, _ = state
    sid, bid = created["session_id"], created["branch_id"]
    append(sessions, bid, "已提交消息")
    path = sessions.journal.path(sid)
    committed = path.read_bytes()
    broken = b'{"incomplete":"\xe4\xb8'
    with path.open("ab") as stream:
        stream.write(broken)
    assert sessions.path(bid)[-1]["data"]["content"] == "已提交消息"
    assert path.read_bytes() == committed
    assert next((sessions.journal.directory / "recovery").glob("*.tail")).read_bytes() == broken
    assert (
        store.one("SELECT recovery_note FROM session_logs WHERE session_id=?", (sid,))[
            "recovery_note"
        ]
        == "uncommitted_tail_recovered"
    )


@pytest.mark.parametrize(
    "damage", ["committed_truncation", "valid_json_tamper", "bad_complete_tail"]
)
async def test_corrupt_committed_data_is_not_silently_repaired_or_taken_from_sql(state, damage):
    _, _, sessions, _, created, _, _ = state
    bid, sid = created["branch_id"], created["session_id"]
    append(sessions, bid, "原文甲")
    path = sessions.journal.path(sid)
    original = path.read_bytes()
    if damage == "committed_truncation":
        altered = original[:-1]
    elif damage == "valid_json_tamper":
        altered = original.replace("原文甲".encode(), "原文乙".encode())
    else:
        altered = original + b"not-json\n"
    path.write_bytes(altered)
    with pytest.raises(Conflict):
        sessions.path(bid)
    assert path.read_bytes() == altered


async def test_metadata_rebuild_replays_branch_lifecycle_without_resurrecting_pruned_tail(state):
    store, _, sessions, memory, created, _, _ = state
    sid, bid = created["session_id"], created["branch_id"]
    shared = append(sessions, bid, "共同祖先")
    other = sessions.fork(bid, shared)
    removed = append(sessions, bid, "已剪枝尾部", "assistant")
    kept = append(sessions, other, "保留分支尾部", "assistant")
    sessions.delete_branch(bid)
    before = sessions.path(other)
    count = len(lines(sessions, sid))
    sessions.rebuild(sid)
    assert sessions.path(other) == before and sessions.path(other)[-1]["id"] == kept
    assert not store.all("SELECT id FROM entries WHERE id=?", (removed,))
    assert not memory.search_sessions("已剪枝")
    assert len(lines(sessions, sid)) == count


async def test_deleted_session_stays_deleted_when_metadata_is_rebuilt(state):
    store, _, sessions, memory, created, _, _ = state
    sid, bid = created["session_id"], created["branch_id"]
    append(sessions, bid, "不能复活的会话")
    sessions.delete_branch(bid)
    count = len(lines(sessions, sid))
    assert sessions.journal.path(sid).exists()
    sessions.rebuild(sid)
    assert store.all("SELECT * FROM sessions WHERE id=?", (sid,)) == []
    assert memory.search_sessions("不能复活") == []
    assert len(lines(sessions, sid)) == count
    with pytest.raises(NotFound):
        sessions.events(sid)


async def test_cascade_deletion_has_a_log_tombstone_and_never_recreates_owner(state):
    store, _, sessions, _, created, tid, _ = state
    sid = created["session_id"]
    store.delete("topics", tid)
    sessions.flush_deleted()
    assert lines(sessions, sid)[-1]["type"] == "session_deleted"
    Sessions(store)
    assert store.all("SELECT * FROM sessions") == []


async def test_discussion_logs_actual_event_stream_and_run_metadata(state):
    store, service, sessions, _, created, _, tm = state
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    service.provider = ScriptedProvider(
        Turn(
            calls=[
                ToolCall(
                    "read-1",
                    "material_read",
                    json.dumps({"material_id": mid, "start": 0, "length": 4000}),
                )
            ],
            finish_reason="tool_calls",
        ),
        '{"status":"succeeded","reason":"完成","answer":"实际读取后回答。"}',
    )
    discussion = Discussion(store, sessions, service)
    bid, sid = created["branch_id"], created["session_id"]
    discussion.start(bid, "请阅读", expected_leaf=sessions.path(bid)[-1]["id"])
    run = discussion.runs[bid]
    await run.task
    assert run.phase == "completed", run.error
    events = sessions.events(sid, run_id=run.id)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "agent.run_started" and kinds[-1] == "agent.run_finished"
    assert kinds.index("agent.tool_requested") < kinds.index("agent.tool_result")
    assert "agent.text_delta" in kinds and "agent.harness_ended" in kinds
    metadata = store.one("SELECT * FROM session_runs WHERE id=?", (run.id,))
    assert metadata["status"] == "completed" and metadata["ended_at"]
    assert len(service.provider.requests) == 2


async def test_restart_marks_running_run_interrupted_without_running_model(state):
    store, service, sessions, _, created, _, _ = state
    rid = identity()
    sessions.record_event(created["branch_id"], rid, "run_started", {})
    sessions.interrupt_runs()
    sessions.interrupt_runs()
    events = sessions.events(created["session_id"], run_id=rid)
    assert [e["type"] for e in events] == ["agent.run_started", "agent.run_interrupted"]
    assert (
        store.one("SELECT status FROM session_runs WHERE id=?", (rid,))["status"] == "interrupted"
    )
    assert service.provider.requests == []


async def test_session_write_lock_and_invalid_payload_do_not_append_a_line(state):
    _, _, sessions, _, created, _, _ = state
    bid, sid = created["branch_id"], created["session_id"]
    path = sessions.journal.path(sid)
    before = path.read_bytes()
    with FileLock(sessions.journal.directory / ".sessions.lock"):
        with pytest.raises(Conflict):
            append(sessions, bid, "不应写入")
    leaf = sessions.path(bid)[-1]["id"]
    with pytest.raises(ValueError):
        sessions.append(bid, leaf, [("message", {"role": "user", "content": float("nan")})])
    assert path.read_bytes() == before


async def test_jsonl_event_api_is_authenticated_paged_and_filterable(tmp_path):
    seed(str(tmp_path))
    provider = ScriptedProvider()
    app = create_app(directory=tmp_path, token="events-test", provider=provider)
    sessions = app.state.sessions
    tm = app.state.store.one("SELECT id FROM topic_materials")["id"]
    created = sessions.create(tm)
    rid = identity()
    sessions.record_event(created["branch_id"], rid, "run_started", {})
    sessions.record_event(
        created["branch_id"], rid, "run_finished", {"status": "completed", "error": None}
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            url = "/api/sessions/" + created["session_id"] + "/events"
            assert (await client.get(url)).status_code == 401
            client.headers["authorization"] = "Bearer events-test"
            first = (await client.get(url, params={"limit": 1})).json()
            following = (
                await client.get(url, params={"after": first[0]["sequence"], "run_id": rid})
            ).json()
            assert first[0]["type"] == "session_created" and len(following) == 2
            assert (await client.get(url, params={"after": -1})).status_code == 422
            assert (await client.get(url, params={"limit": 201})).status_code == 422
    assert provider.requests == []
