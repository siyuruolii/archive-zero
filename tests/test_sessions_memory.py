import asyncio

import pytest
from conftest import ScriptedProvider

from reading_agent.services.discussion import Discussion
from reading_agent.storage.database import Conflict


def append(sessions, bid, text, role="user"):
    return sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": role, "content": text})]
    )


async def test_branch_isolation_pruning_and_last_session(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    shared = append(sessions, bid, "共同问题")
    other = sessions.fork(bid, shared)
    append(sessions, bid, "甲分支", "assistant")
    append(sessions, other, "乙分支", "assistant")
    assert "甲分支" not in str(sessions.context(other))
    sessions.delete_branch(bid)
    assert sessions.path(other)[-1]["data"]["content"] == "乙分支"
    sessions.delete_branch(other)
    assert store.all("SELECT * FROM sessions") == []
    assert store.all("SELECT * FROM entries") == []
    assert store.one("SELECT id FROM topic_materials WHERE id=?", (tm,))


async def test_expected_leaf_and_stable_root(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    root = sessions.path(bid)[0]
    append(sessions, bid, "问题")
    with pytest.raises(Conflict):
        sessions.append(bid, root["id"], [("message", {"role": "user", "content": "旧请求"})])
    with store.transaction() as db:
        db.execute("UPDATE topic_materials SET deep_summary='后来精读结果' WHERE id=?", (tm,))
    assert sessions.path(bid)[0] == root


async def test_saved_discussion_and_event_commit_order(state):
    store, service, sessions, memory, created, tid, tm = state
    service.provider = ScriptedProvider(
        '{"status":"succeeded","reason":"回答完成","answer":"程序检查权限后执行。"}'
    )
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    leaf = sessions.path(bid)[-1]["id"]
    discussion.start(bid, "工具是什么？", expected_leaf=leaf)
    run = discussion.runs[bid]
    with pytest.raises(Conflict):
        discussion.start(bid, "重复", expected_leaf=leaf)
    await run.task
    assert run.phase == "completed", run.error
    assert sessions.path(bid)[-1]["data"]["role"] == "assistant"


async def test_memory_scope_correction_delete_and_independent_evidence(state):
    store, service, sessions, memory, created, tid, tm = state
    other = store.topic("其他")
    local = memory.add("我熟悉 Java", topic_id=tid, evidence=[{"type": "topic_material", "id": tm}])
    glob = memory.add("我通用偏好简短解释")
    assert {r["id"] for r in memory.list(other)} == {glob}
    new = memory.correct(local, "我刚开始学习 Java")
    assert local not in {r["id"] for r in memory.list(tid)}
    assert new in {r["id"] for r in memory.list(tid)}
    experience = memory.experience(tm, "read")
    store.delete("topic_materials", tm)
    assert memory.evidence(experience)[0]["accessible"] is False
    assert memory.list(tid)
    store.delete("topics", tid)
    assert memory.list(tid) == []
    assert store.one("SELECT * FROM memories WHERE id=?", (new,))
    memory.delete(new)
    assert not store.all("SELECT * FROM memories WHERE id=?", (new,))


async def test_memory_extraction_uses_user_quote_and_topic_scope(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    append(sessions, bid, "我熟悉Java，正在学习Python")
    append(sessions, bid, "收到", "assistant")
    memory.provider = ScriptedProvider(
        '{"profiles":[{"content":"用户熟悉Java","quote":"我熟悉Java","explicit_global":true},{"content":"用户掌握Python","quote":"不存在的原话","explicit_global":false}]}'
    )
    await memory.extract(bid, "我熟悉Java，正在学习Python")
    rows = memory.list(tid)
    assert len(rows) == 1 and rows[0]["scope"] == "topic"
    assert rows[0]["content"] == "用户熟悉Java"


async def test_bounded_events_snapshot_and_stream_disconnect(state):
    store, service, sessions, memory, created, tid, tm = state
    from reading_agent.services.discussion import Running

    run = Running()
    queue = asyncio.Queue(maxsize=128)
    run.subscribers.add(queue)
    for i in range(200):
        run.emit("status", message=str(i))
    assert 0 < queue.qsize() <= 128 and run.snapshot()["sequence"] == 200
    recovered = queue.get_nowait()
    assert recovered["type"] == "snapshot" and recovered["data"]["sequence"] == 129
    assert recovered["data"]["status_message"] == "128"
