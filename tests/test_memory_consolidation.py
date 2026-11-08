"""同步提取与后台 Agent、文件能力、并发提交和恢复的集成验收。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import ScriptedProvider
from test_sessions_memory import append

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider, ProviderEvent, Settings, ToolCall, Turn
from reading_agent.agents.sample import sample_corpus
from reading_agent.agents.tool import ToolSession
from reading_agent.infrastructure.configuration import memory_policy, memory_provider
from reading_agent.infrastructure.locking import FileLock
from reading_agent.services.memory import Memory
from reading_agent.services.memory_consolidation import (
    ConsolidationPolicy,
    MemoryWorkspace,
    load_state,
    relative_path,
    save_state,
)
from reading_agent.storage.database import Conflict

DONE = '{"status":"succeeded","reason":"已检查","summary":"整理完成"}'


def tool(name, args, cid):
    return Turn(
        calls=[ToolCall(cid, name, json.dumps(args, ensure_ascii=False))],
        finish_reason="tool_calls",
    )


def configure(memory, provider, **changes):
    memory.consolidator.provider = provider
    memory.consolidator.policy = ConsolidationPolicy(min_new=1, cooldown_seconds=0, **changes)


def profile_job(memory, tid, contents=("原始画像",), *, patch=True):
    mids = [memory.add(content, topic_id=tid) for content in contents]
    paths = ["user/" + mid + ".md" for mid in mids]
    steps = [tool("memory_file_read", {"paths": paths}, "read")]
    if patch:
        steps.append(
            Turn(
                calls=[
                    ToolCall(
                        "patch" + str(i),
                        "memory_file_patch",
                        json.dumps(
                            {"path": path, "old_text": content, "new_text": "整理后的" + content},
                            ensure_ascii=False,
                        ),
                    )
                    for i, (path, content) in enumerate(zip(paths, contents, strict=True))
                ],
                finish_reason="tool_calls",
            )
        )
    steps.append(DONE)
    return mids, paths, steps


async def test_extraction_uses_separate_small_provider_and_program_writes_before_return(state):
    _, _, sessions, memory, created, tid, _ = state
    bid = created["branch_id"]
    append(sessions, bid, "我熟悉Java")
    append(sessions, bid, "已回答", "assistant")
    main = ScriptedProvider()
    small = ScriptedProvider(
        '{"profiles":[{"content":"用户熟悉Java","quote":"我熟悉Java","explicit_global":false}]}'
    )
    memory.provider, memory.extraction_provider = main, small
    await memory.extract(bid, "我熟悉Java")
    row = memory.list(tid, category="profile")[0]
    assert "Java" in row["content"] and main.requests == []
    assert len(small.requests) == 1 and small.requests[0].tools == []
    assert (memory.directory / "user" / (row["id"] + ".md")).exists()


async def test_background_agent_patches_workspace_then_commits_and_does_not_self_trigger(state):
    _, _, _, memory, _, tid, _ = state
    mids, paths, steps = profile_job(memory, tid)
    configure(memory, ScriptedProvider(*steps))
    assert memory.maybe_consolidate()
    await memory.consolidator.task
    status = memory.consolidator.status()
    assert status["status"] == "completed" and status["processed_count"] == 1
    assert memory.list(tid)[0]["content"] == "整理后的原始画像"
    assert memory.list(tid)[0]["origin"] == "memory_consolidation"
    assert not memory.maybe_consolidate(force=True)
    before = memory.directory / ".consolidation" / "runs" / status["run_id"] / "before" / paths[0]
    assert "整理后的" not in before.read_text(encoding="utf-8")
    reopened = Memory(memory.store, None)
    assert reopened.consolidator.status()["processed_count"] == 1
    assert reopened.consolidator.status()["pending_count"] == 0


async def test_noop_review_is_valid_but_unread_success_is_rejected(state):
    _, _, _, memory, _, tid, _ = state
    mids, _, steps = profile_job(memory, tid, patch=False)
    configure(memory, ScriptedProvider(DONE))
    assert memory.maybe_consolidate()
    await memory.consolidator.task
    assert memory.consolidator.status()["error"] == "consolidation_pending_not_reviewed"
    assert memory.consolidator.status()["processed_count"] == 0
    memory.consolidator.provider = ScriptedProvider(*steps)
    assert memory.maybe_consolidate(force=True)
    await memory.consolidator.task
    assert memory.consolidator.status()["processed_count"] == 1
    assert memory.list(tid)[0]["id"] == mids[0] and memory.list(tid)[0]["content"] == "原始画像"


async def test_threshold_time_fallback_cooldown_and_disabled_policy(state):
    _, _, _, memory, _, tid, _ = state
    memory.add("一个新画像", topic_id=tid)
    manager = memory.consolidator
    manager.policy = ConsolidationPolicy(min_new=3, cooldown_seconds=60, interval_seconds=1000)
    assert not memory.maybe_consolidate()
    with memory._lock():
        status = load_state(memory)
        status["created_at"] = (datetime.now(UTC) - timedelta(seconds=2000)).isoformat()
        save_state(memory, status)
    manager.provider = ScriptedProvider(DONE)
    assert memory.maybe_consolidate()  # 时间满足，即使只有一条新增也可处理。
    await manager.task
    assert not memory.maybe_consolidate()  # 失败仍遵守冷却，不高频重试。
    manager.policy = manager.policy.model_copy(update={"enabled": False})
    assert not memory.maybe_consolidate(force=True)


async def test_snapshot_conflict_keeps_user_correction_even_when_original_file_hash_is_unchanged(
    state,
):
    _, _, _, memory, _, tid, _ = state
    mids, _, steps = profile_job(memory, tid)
    ready, release = asyncio.Event(), asyncio.Event()

    class Pausing(ScriptedProvider):
        async def stream(self, request):
            if len(self.requests) == 2:
                ready.set()
                await release.wait()
            async for event in super().stream(request):
                yield event

    configure(memory, Pausing(*steps))
    assert memory.maybe_consolidate()
    await asyncio.wait_for(ready.wait(), 2)
    replacement = memory.correct(mids[0], "用户明确更正的新画像")
    release.set()
    await memory.consolidator.task
    status = memory.consolidator.status()
    assert status["status"] == "conflict" and status["processed_count"] == 0
    assert memory.list(tid)[0]["id"] == replacement
    assert memory.list(tid)[0]["content"] == "用户明确更正的新画像"


async def test_foreground_new_memory_is_preserved_and_excluded_from_current_watermark(state):
    _, _, _, memory, _, tid, _ = state
    _, _, steps = profile_job(memory, tid)
    ready, release = asyncio.Event(), asyncio.Event()

    class Pausing(ScriptedProvider):
        async def stream(self, request):
            if len(self.requests) == 2:
                ready.set()
                await release.wait()
            async for event in super().stream(request):
                yield event

    configure(memory, Pausing(*steps))
    memory.maybe_consolidate()
    await asyncio.wait_for(ready.wait(), 2)
    new_id = memory.add("并发新增画像", topic_id=tid)  # 模型等待时 .memory.lock 可取得。
    another = Memory(memory.store, None)
    assert another.consolidator.status()["status"] == "running"
    assert not another.maybe_consolidate(force=True)
    release.set()
    await memory.consolidator.task
    status = memory.consolidator.status()
    assert status["processed_count"] == 1 and status["pending_count"] == 1
    assert new_id not in status["processed_ids"]


async def test_commit_failure_rolls_forward_before_exposing_partially_published_batch(
    state, monkeypatch
):
    _, _, _, memory, _, tid, _ = state
    _, paths, steps = profile_job(memory, tid, ("第一条画像", "第二条画像"))
    configure(memory, ScriptedProvider(*steps))
    original = memory._atomic
    journal = memory.directory / ".consolidation" / "commit.json"
    target = memory.directory / paths[1]

    def fail(path, text):
        if path == target and journal.exists():
            raise OSError("simulated commit interruption")
        return original(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(memory, "_atomic", fail)
        memory.maybe_consolidate()
        await memory.consolidator.task
        assert journal.exists()
    memory.rebuild()
    assert not journal.exists()
    assert {r["content"] for r in memory.list(tid)} == {"整理后的第一条画像", "整理后的第二条画像"}
    assert memory.consolidator.status()["status"] == "completed"
    assert memory.consolidator.status()["processed_count"] == 2


async def test_recovery_does_not_accept_deleted_unchanged_source(state):
    _, _, _, memory, _, tid, _ = state
    mids, paths, _ = profile_job(memory, tid, ("第一条画像", "第二条画像"))
    with memory._lock():
        documents = memory._sync()
        workspace = MemoryWorkspace(memory, "recovery-test", list(documents.values()), mids)
        workspace.read(paths)
        workspace.patch(paths[0], "第一条画像", "整理后的第一条画像")
        journal = memory.directory / ".consolidation" / "commit.json"
        memory._atomic(journal, json.dumps(workspace.plan(), ensure_ascii=False))
        (memory.directory / paths[1]).unlink()
    with pytest.raises(Conflict, match="source changed"):
        memory.rebuild()
    assert "整理后的" not in (memory.directory / paths[0]).read_text(encoding="utf-8")


async def test_file_capability_is_workspace_only_and_reading_events_are_immutable(state):
    store, _, _, memory, _, tid, tm = state
    profile = memory.add("用户画像", topic_id=tid)
    event = memory.experience(tm, "read")
    with memory._lock():
        documents = memory._sync()
        workspace = MemoryWorkspace(memory, "tool-test", list(documents.values()), [profile, event])
    paths = {d.id: relative_path(d) for d in documents.values()}
    with pytest.raises(ValueError, match="read_before_write"):
        workspace.patch(paths[profile], "用户画像", "提前修改")
    workspace.read(list(paths.values()))
    with pytest.raises(ValueError, match="path_not_allowed"):
        workspace.read(["../../model.json"])
    with pytest.raises(ValueError, match="reading_event_immutable"):
        workspace.patch(paths[event], "用户曾明确标记该材料已读", "用户已掌握材料")
    text = (workspace.root / "work" / paths[profile]).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="metadata_changed"):
        workspace.write(paths[profile], text.replace(tid, store.topic("其他")))
    corpus = sample_corpus()
    normal = ToolSession(
        corpus, Task(role="discuss", material_id="tools", topic_ids=["learn"], question="提问")
    )
    assert (
        normal.execute("memory_file_read", '{"paths":["user/x.md"]}')["error"] == "tool_not_allowed"
    )


async def test_create_summary_preserves_raw_events_and_excludes_curator_output_from_new_count(
    state,
):
    _, _, _, memory, _, tid, tm = state
    event = memory.experience(tm, "read")
    path = "user-reading/entries/" + event + ".md"
    steps = [
        tool("memory_file_read", {"paths": [path]}, "read"),
        tool(
            "memory_file_create",
            {
                "category": "user_reading",
                "topic_id": tid,
                "source_ids": [event],
                "content": "用户曾标记材料已读，理解程度未知。",
            },
            "create",
        ),
        DONE,
    ]
    configure(memory, ScriptedProvider(*steps))
    memory.maybe_consolidate()
    await memory.consolidator.task
    rows = memory.list(tid, category="user_reading")
    assert len(rows) == 2 and any(
        r["id"] == event and r["origin"] == "business_event" for r in rows
    )
    assert memory.consolidator.status()["pending_count"] == 0
    assert not memory.maybe_consolidate(force=True)


async def test_budget_failure_does_not_commit_workspace_or_advance_checkpoint(state):
    _, _, _, memory, _, tid, _ = state
    _, _, steps = profile_job(memory, tid)
    configure(memory, ScriptedProvider(*steps), max_model_calls=1)
    memory.maybe_consolidate()
    await memory.consolidator.task
    status = memory.consolidator.status()
    assert status["status"] == "failed" and status["processed_count"] == 0
    assert status["error"] == "consolidation_agent_model_budget_exhausted"
    assert memory.list(tid)[0]["content"] == "原始画像"


async def test_shutdown_before_task_starts_releases_lease_and_records_interruption(state):
    _, _, _, memory, _, tid, _ = state
    memory.add("尚未整理的画像", topic_id=tid)
    configure(memory, ScriptedProvider())
    assert memory.maybe_consolidate()
    await memory.shutdown()
    assert memory.consolidator.status()["status"] == "interrupted"
    with FileLock(memory.directory / ".consolidation" / "runner.lock"):
        pass


async def test_harness_awaits_extraction_but_returns_while_consolidation_is_running(state):
    store, service, sessions, memory, created, tid, tm = state
    started, release, background_started, closed = (asyncio.Event() for _ in range(4))

    class Small:
        async def stream(self, request):
            started.set()
            await release.wait()
            yield ProviderEvent(
                "turn",
                turn=Turn(
                    content='{"profiles":[{"content":"用户熟悉Java","quote":"我熟悉Java","explicit_global":false}]}'
                ),
            )

    class Heavy:
        async def stream(self, request):
            background_started.set()
            try:
                await asyncio.Event().wait()
                yield ProviderEvent("turn", turn=Turn(content=DONE))
            finally:
                closed.set()

    memory.extraction_provider = Small()
    configure(memory, Heavy())
    bid = created["branch_id"]
    leaf = append(sessions, bid, "我熟悉Java")
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]

    def commit(report):
        sessions.append(
            bid, leaf, [("message", {"role": "assistant", "content": report.result["answer"]})]
        )

    pending = asyncio.create_task(
        Harness(
            ScriptedProvider('{"status":"succeeded","reason":"完成","answer":"回答"}'),
            service.corpus(mid, [tid]),
            memory=memory,
            sessions=sessions,
        ).run(
            Task(role="discuss", material_id=mid, topic_ids=[tid], question="我熟悉Java"),
            branch_id=bid,
            question_entry_id=leaf,
            commit=commit,
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    assert not pending.done() and memory.list(tid, category="profile") == []
    release.set()
    report = await asyncio.wait_for(pending, 2)
    await asyncio.wait_for(background_started.wait(), 2)
    assert report.memory_status == "completed" and report.consolidation_scheduled
    assert memory.list(tid, category="profile") and not memory.consolidator.task.done()
    await memory.shutdown()
    assert closed.is_set()


async def test_model_and_policy_environment_can_be_configured_independently(monkeypatch):
    settings = Settings(
        api_key="test-not-a-real-key", model="main", base_url="https://example.test"
    )
    async with httpx.AsyncClient() as client:
        main = ChatProvider(settings, client)
        monkeypatch.setenv("AGENT_MEMORY_EXTRACT_MODEL", "small")
        monkeypatch.setenv("AGENT_MEMORY_CONSOLIDATE_MODEL", "heavy")
        assert memory_provider(main, "extract").model_name == "small"
        assert memory_provider(main, "consolidate").model_name == "heavy"
        assert main.model_name == "main"
        with monkeypatch.context() as patch:
            patch.setenv("AGENT_MEMORY_EXTRACT_API_KEY", "sensitive-test-credential")
            patch.setenv("AGENT_MEMORY_EXTRACT_BASE_URL", "http://remote.example.test")
            with pytest.raises(ValueError) as failure:
                memory_provider(main, "extract")
            assert "sensitive-test-credential" not in str(failure.value)
        monkeypatch.setenv("AGENT_MEMORY_CONSOLIDATE_MIN_NEW", "42")
        assert memory_policy().min_new == 42
        monkeypatch.setenv("AGENT_MEMORY_CONSOLIDATE_ENABLED", "maybe")
        with pytest.raises(ValueError):
            memory_policy()


async def test_quantity_threshold_runs_only_when_new_count_reaches_minimum(state):
    _, _, _, memory, _, tid, _ = state
    memory.consolidator.policy = ConsolidationPolicy(min_new=2, cooldown_seconds=0)
    first = memory.add("第一条新增", topic_id=tid)
    assert not memory.maybe_consolidate()
    second = memory.add("第二条新增", topic_id=tid)
    paths = ["user/" + mid + ".md" for mid in (first, second)]
    memory.consolidator.provider = ScriptedProvider(
        tool("memory_file_read", {"paths": paths}, "read"), DONE
    )
    assert memory.maybe_consolidate()
    await memory.consolidator.task
    assert memory.consolidator.status()["processed_count"] == 2


async def test_crashed_running_state_is_recovered_without_losing_checkpoint(state):
    _, _, _, memory, _, tid, _ = state
    memory.add("待处理", topic_id=tid)
    with memory._lock():
        status = load_state(memory)
        status.update(status="running", run_id="old-run", processed_ids=["previously-processed"])
        save_state(memory, status)
    reopened = Memory(memory.store, None)
    assert reopened.consolidator.status()["status"] == "interrupted"
    assert reopened.consolidator.status()["processed_ids"] == ["previously-processed"]


async def test_snapshot_creation_failure_is_persistent_and_does_not_leave_ownership_lock(
    state, monkeypatch
):
    _, _, _, memory, _, tid, _ = state
    memory.add("待整理", topic_id=tid)
    configure(memory, ScriptedProvider())
    original = memory._atomic

    def fail(path, text):
        if "work" in path.parts:
            raise OSError("snapshot failure")
        return original(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(memory, "_atomic", fail)
        with pytest.raises(OSError):
            memory.maybe_consolidate()
    status = memory.consolidator.status()
    assert status["status"] == "failed" and status["error"] == "OSError"
    assert (memory.directory / ".consolidation" / "runs" / status["run_id"] / "error.json").exists()
    with FileLock(memory.directory / ".consolidation" / "runner.lock"):
        pass


async def test_merge_cycles_and_cross_topic_aggregation_are_rejected(state):
    store, _, _, memory, _, tid, _ = state
    a = memory.add("同范围一", topic_id=tid)
    b = memory.add("同范围二", topic_id=tid)
    other = store.topic("其他范围")
    c = memory.add("其他范围", topic_id=other)
    global_id = memory.add("全局背景")
    with memory._lock():
        docs = memory._sync()
        workspace = MemoryWorkspace(memory, "merge-test", list(docs.values()), [a, b, c])
    paths = {mid: relative_path(docs[mid]) for mid in (a, b, c)}
    workspace.read(list(paths.values()))
    workspace.read([relative_path(docs[global_id])])
    with pytest.raises(ValueError, match="source_scope"):
        workspace.create("profile", "unknown-topic", [global_id], "无依据的主题归属")
    with pytest.raises(ValueError, match="source_scope"):
        workspace.create("profile", None, [c], "不能提升为全局画像")
    workspace.patch(paths[a], '"superseded_by": null', '"superseded_by": "' + b + '"')
    workspace.patch(paths[b], '"superseded_by": null', '"superseded_by": "' + a + '"')
    with pytest.raises(ValueError, match="invalid_merge"):
        workspace.plan()
