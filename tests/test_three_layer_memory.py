"""MD 权威存储、实际阅读凭据和跨会话检索的集成契约。"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import ScriptedProvider
from test_sessions_memory import append

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ProviderError, ProviderEvent, ToolCall, Turn
from reading_agent.agents.tool import ToolSession
from reading_agent.infrastructure.locking import FileLock
from reading_agent.interfaces.api import create_app
from reading_agent.services.memory import SCHEMA, Memory, SessionSummary
from reading_agent.storage.database import Conflict, Store, identity, now

ANSWER = '{"status":"succeeded","reason":"完成","answer":"根据引用的历史讨论回答。"}'


def document_path(memory, mid):
    return next(memory.directory.rglob(f"{mid}.md"))


async def test_markdown_is_authoritative_and_index_can_be_rebuilt(state):
    store, _, _, memory, _, tid, _ = state
    mid = memory.add("用户熟悉Java", topic_id=tid)
    path = document_path(memory, mid)
    assert "用户熟悉Java" in path.read_text(encoding="utf-8")
    with store.transaction() as db:
        db.execute("DELETE FROM memories")
        db.execute("DELETE FROM memory_documents")
    assert memory.rebuild() == 1
    assert memory.list(tid)[0]["id"] == mid
    path.write_text(
        path.read_text(encoding="utf-8").replace("用户熟悉Java", "用户正在学习Python"),
        encoding="utf-8",
    )
    assert memory.list(tid)[0]["content"] == "用户正在学习Python"
    reopened = Memory(store, ScriptedProvider())
    assert reopened.list(tid)[0]["content"] == "用户正在学习Python"


def test_legacy_migration_preserves_ids_and_does_not_resurrect_deleted_records(tmp_path):
    store = Store(tmp_path / "legacy.db")
    mid = identity()
    with store.transaction() as db:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO memories VALUES(?, 'profile','global',NULL,?,"
            "'user_statement','[]',NULL,?,?)",
            (mid, "用户偏好简洁", now(), now()),
        )
    memory = Memory(store, ScriptedProvider())
    assert memory.list()[0]["id"] == mid
    replacement = memory.correct(mid, "用户偏好详细")
    memory.delete(replacement)
    assert memory.list() == []
    reopened = Memory(store, ScriptedProvider())
    assert reopened.list() == []
    assert document_path(reopened, replacement).read_text(encoding="utf-8").endswith("---\n\n\n")
    assert "用户偏好详细" not in document_path(reopened, replacement).read_text(encoding="utf-8")


def test_different_databases_do_not_share_default_markdown_directory(tmp_path):
    first = Memory(Store(tmp_path / "first.db"), None)
    second = Memory(Store(tmp_path / "second.db"), None)
    first.add("只属于第一个数据库的用户画像")
    assert first.directory != second.directory
    assert second.list() == []


async def test_atomic_write_failure_preserves_original_and_cleans_temporary_file(
    state, monkeypatch
):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add("原始画像", topic_id=tid)
    original = document_path(memory, mid).read_bytes()

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("reading_agent.services.memory.os.replace", fail)
    with pytest.raises(OSError):
        memory.correct(mid, "不能提交的更正")
    assert document_path(memory, mid).read_bytes() == original
    assert memory.list(tid)[0]["content"] == "原始画像"
    assert not list(memory.directory.glob("**/*.tmp"))


async def test_corrupt_markdown_is_visible_and_does_not_destroy_index(state):
    store, _, _, memory, _, tid, _ = state
    mid = memory.add("可恢复的画像", topic_id=tid)
    document_path(memory, mid).write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        memory.rebuild()
    assert store.one("SELECT content FROM memories WHERE id=?", (mid,))["content"] == "可恢复的画像"


async def test_conflicting_writer_is_explicit_not_a_lost_update(state):
    _, _, _, memory, _, tid, _ = state
    with FileLock(memory.directory / ".memory.lock"):
        with pytest.raises(Conflict):
            memory.add("不能丢失的更改", topic_id=tid)
    assert memory.list(tid) == []


async def test_agent_reading_versions_coverage_and_user_actions_are_independent(state):
    _, service, _, memory, _, tid, tm = state
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    task = Task(role="discuss", material_id=mid, topic_ids=[tid], question="讨论")
    corpus = service.corpus(mid, [tid])
    corpus.materials[mid].body = "abcdefghij"

    def report(start, end):
        return SimpleNamespace(
            readings=[{"material_id": mid, "start": start, "end": end}], status="failed"
        )

    first = memory.remember_readings(task, corpus, report(0, 4))[0]
    assert memory.remember_readings(task, corpus, report(4, 10)) == [first]
    rows = memory.list(tid, category="agent_reading")
    assert rows[0]["details"]["coverage"] == [[0, 10]] and rows[0]["details"]["complete"]
    assert rows[0]["details"]["content_hash"] == hashlib.sha256(b"abcdefghij").hexdigest()
    assert memory.list(tid, category="user_reading") == []
    corpus.materials[mid].body = "changed version"
    assert memory.remember_readings(task, corpus, report(0, 3))[0] != first
    assert len(memory.list(tid, category="agent_reading")) == 2
    memory.experience(tm, "read")
    action = memory.list(tid, category="user_reading")[0]
    assert action["details"] == {"action": "read", "material_id": mid}
    assert len(memory.list(tid, category="agent_reading")) == 2


async def test_reading_memory_cannot_satisfy_new_deep_read_evidence(state):
    _, service, _, memory, _, tid, tm = state
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    corpus = service.corpus(mid, [tid])
    task = Task(role="deep_read", material_id=mid, topic_ids=[tid], deep_read_guideline="阅读")
    provider = ScriptedProvider(
        json.dumps(
            {
                "status": "succeeded",
                "reason": "历史读过",
                "deep_summary": "不能充当新证据",
                "coverage": [{"material_id": mid, "start": 0, "end": 1}],
                "limitations": "",
            }
        )
    )
    memory.add("曾读过材料", topic_id=tid, kind="agent_reading", details={"material_id": mid})
    report = await Harness(provider, corpus, memory=memory).run(task)
    assert report.status == "failed" and report.end_reason == "invalid_final_result"


@pytest.mark.parametrize("query", ["上下文压缩", "死锁", "Harness", "AI", '"Harness" OR ("'])
async def test_bilingual_keyword_search_and_safe_query_literals(state, query):
    _, _, sessions, memory, created, tid, _ = state
    bid = created["branch_id"]
    append(sessions, bid, "这里讨论 Harness 的上下文压缩与死锁问题，以及AI助手。")
    append(sessions, bid, "最后决定保留读取证据。", "assistant")
    results = memory.search_sessions(query, topic_id=tid)
    assert results and results[0]["session_id"] == created["session_id"]
    assert "读取证据" in results[0]["snippet"]
    assert memory.search_sessions("从未出现过的完整短语") == []


async def test_session_search_excludes_current_session_and_keeps_branches_separate(state):
    _, _, sessions, memory, created, tid, _ = state
    bid = created["branch_id"]
    shared = append(sessions, bid, "公共讨论 Harness")
    other = sessions.fork(bid, shared)
    append(sessions, bid, "Harness 甲分支决定使用同步提交。", "assistant")
    append(sessions, other, "Harness 乙分支决定使用异步提交。", "assistant")
    results = memory.search_sessions("Harness")
    assert len(results) == 1
    text = results[0]["snippet"]
    assert not ("甲分支" in text and "乙分支" in text)
    assert memory.search_sessions("Harness", current_session_id=created["session_id"]) == []
    assert memory.search_sessions("Harness", topic_id="nonexistent") == []


async def test_indexes_track_edits_deletion_and_rebuild(state):
    store, _, sessions, memory, created, _, _ = state
    bid = created["branch_id"]
    eid = append(sessions, bid, "FTSoriginal")
    assert memory.search_sessions("FTSoriginal")
    sessions.revise(eid, {"role": "user", "content": "FTSchanged"})
    assert not memory.search_sessions("FTSoriginal")
    assert memory.search_sessions("FTSchanged")
    with store.transaction() as db:
        db.execute("DELETE FROM session_memory_words")
        db.execute("DELETE FROM session_memory_trigrams")
    assert not memory.search_sessions("FTSchanged")
    assert memory.rebuild_sessions() == 1
    assert memory.search_sessions("FTSchanged")
    sessions.delete_branch(bid)
    assert memory.search_sessions("FTSchanged") == []
    assert store.all("SELECT * FROM session_memory_words") == []


async def test_long_message_search_keeps_match_window(state):
    _, _, sessions, memory, created, _, _ = state
    append(sessions, created["branch_id"], "前文" * 3000 + "目标词Harness方案" + "后文" * 3000)
    result = memory.search_sessions("Harness")[0]
    assert "目标词Harness方案" in result["snippet"]
    assert len(result["messages"][0]["content"]) <= 1800


@pytest.mark.parametrize("bad", [False, True])
async def test_query_focused_summary_requires_grounded_citations(state, bad):
    _, _, sessions, memory, created, _, _ = state
    eid = append(sessions, created["branch_id"], "Harness 保存成功后才能提炼记忆。")

    async def summarize(schema, instruction, data):
        assert data["query"] == "Harness"
        return SessionSummary(
            summary="之前确认了提交顺序。",
            evidence=[{"entry_id": eid, "quote": "虚构内容" if bad else "保存成功后才能提炼记忆"}],
        )

    result = (await memory.recall_sessions("Harness", summarize=summarize))[0]
    assert result["recall_status"] == ("snippet_only" if bad else "summarized")
    assert result["summary"] == (None if bad else "之前确认了提交顺序。")


async def test_summary_failure_returns_snippets_and_zero_hits_do_not_call_model(state):
    _, _, sessions, memory, created, _, _ = state
    append(sessions, created["branch_id"], "Harness 历史讨论")
    memory.provider = ScriptedProvider(ProviderError("http_500"))
    result = (await memory.recall_sessions("Harness"))[0]
    assert result["summary"] is None and result["summary_error"] == "ProviderError"
    assert "历史讨论" in result["snippet"]
    provider = ScriptedProvider()
    memory.provider = provider
    assert await memory.recall_sessions("完全没有命中的词") == []
    assert provider.requests == []
    assert (await memory.recall_sessions(""))[0]["recall_status"] == "snippet_only"
    assert provider.requests == []


async def test_deleted_session_is_not_returned_after_summary_await(state):
    _, _, sessions, memory, created, _, _ = state
    bid = created["branch_id"]
    eid = append(sessions, bid, "Harness 依据")

    async def summarize(*args):
        sessions.delete_branch(bid)
        return SessionSummary(
            summary="不能返回已删除来源", evidence=[{"entry_id": eid, "quote": "Harness"}]
        )

    assert await memory.recall_sessions("Harness", summarize=summarize) == []


async def test_changed_source_is_not_returned_after_summary_await(state):
    store, _, sessions, memory, created, _, _ = state
    eid = append(sessions, created["branch_id"], "Harness 原始决定")

    async def summarize(*args):
        sessions.revise(eid, {"role": "user", "content": "修改后的决定"})
        return SessionSummary(
            summary="旧结果不应返回", evidence=[{"entry_id": eid, "quote": "原始决定"}]
        )

    assert await memory.recall_sessions("Harness", summarize=summarize) == []


async def test_session_tool_cache_is_invalidated_when_source_deleted(state):
    _, service, sessions, memory, created, tid, tm = state
    append(sessions, created["branch_id"], "Harness 历史讨论")
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    corpus = service.corpus(mid, [tid])
    memory.bind_sessions(corpus)
    memory.provider = ScriptedProvider(ProviderError("unavailable"))
    tool = ToolSession(
        corpus, Task(role="discuss", material_id=mid, topic_ids=[tid], question="回忆")
    )
    args = json.dumps({"query": "Harness", "topic_id": None, "limit": 1})
    assert (await tool.aexecute("session_search", args))["data"]
    sessions.delete_branch(created["branch_id"])
    assert (await tool.aexecute("session_search", args))["data"] == []


async def test_harness_records_actual_reading_and_preloads_next_context(state):
    _, service, _, memory, _, tid, tm = state
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    task = Task(role="discuss", material_id=mid, topic_ids=[tid], question="阅读")
    corpus = service.corpus(mid, [tid])
    corpus.materials[mid].body = "经过实际工具读取的正文。"
    provider = ScriptedProvider(
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
        ANSWER,
    )
    report = await Harness(provider, corpus, memory=memory).run(task)
    assert report.status == "succeeded" and report.reading_memory_status == "completed"
    rows = memory.list(tid, category="agent_reading")
    assert len(rows) == 1 and rows[0]["details"]["complete"]
    prepared = Harness(None, corpus, memory=memory).prepare(task)
    assert prepared.session.reads == []
    assert prepared.session.corpus.reading_memories[tid][0]["current_version_verified"]
    corpus.materials[mid].body = "变化后的正文"
    prepared = Harness(None, corpus, memory=memory).prepare(task)
    assert not prepared.session.corpus.reading_memories[tid][0]["current_version_verified"]


async def test_session_search_api_is_authenticated_bounded_and_model_free(tmp_path):
    provider = ScriptedProvider()
    app = create_app(directory=tmp_path, token="test", provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/api/session-memory/search")).status_code == 401
            client.headers["authorization"] = "Bearer test"
            assert (
                await client.get("/api/session-memory/search", params={"query": "Harness"})
            ).json() == []
            assert (
                await client.get("/api/session-memory/search", params={"limit": 6})
            ).status_code == 422
            assert (
                await client.get("/api/session-memory/search", params={"query": "x" * 201})
            ).status_code == 422
    assert provider.requests == []


def recall_turn():
    return Turn(
        calls=[
            ToolCall(
                "recall-1",
                "session_search",
                json.dumps({"query": "Harness", "topic_id": None, "limit": 1}),
            )
        ],
        finish_reason="tool_calls",
    )


@pytest.mark.parametrize("max_calls", [2, 3])
async def test_recall_summary_shares_loop_budget_and_does_not_leak_draft(state, max_calls):
    _, service, sessions, memory, created, tid, tm = state
    eid = append(sessions, created["branch_id"], "Harness 需要先提交再提炼记忆。")
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    provider = ScriptedProvider(
        recall_turn(),
        json.dumps(
            {
                "summary": "辅助摘要不得显示为回答",
                "evidence": [{"entry_id": eid, "quote": "先提交再提炼记忆"}],
            }
        ),
        ANSWER,
    )
    events = []
    report = await Harness(
        provider, service.corpus(mid, [tid]), Budget(max_model_calls=max_calls), memory=memory
    ).run(
        Task(role="discuss", material_id=mid, topic_ids=[tid], question="回忆Harness讨论"),
        on_event=events.append,
    )
    assert [a["purpose"] for a in report.attempts][:2] == ["agent", "session_recall"]
    assert report.model_calls == max_calls
    assert report.status == ("succeeded" if max_calls == 3 else "failed")
    if max_calls == 2:
        assert report.end_reason == "model_budget_exhausted"
    assert not any("辅助摘要" in e.data.get("text", "") for e in events)


async def test_cancel_during_recall_closes_auxiliary_stream(state):
    _, service, sessions, memory, created, tid, tm = state
    append(sessions, created["branch_id"], "Harness 历史")
    mid = service.store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))[
        "material_id"
    ]
    started, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingRecall:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ProviderEvent("turn", turn=recall_turn())
                return
            started.set()
            try:
                yield ProviderEvent("text", text="不能泄漏的辅助摘要")
                await asyncio.Event().wait()
            finally:
                closed.set()

    pending = asyncio.create_task(
        Harness(BlockingRecall(), service.corpus(mid, [tid]), memory=memory).run(
            Task(role="discuss", material_id=mid, topic_ids=[tid], question="回忆"), cancel=cancel
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    cancel.set()
    report = await asyncio.wait_for(pending, 1)
    assert closed.is_set() and report.end_reason == "cancelled"
    assert "辅助摘要" not in report.draft
