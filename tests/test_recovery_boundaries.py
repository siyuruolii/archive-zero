import asyncio

import pytest
from conftest import ScriptedProvider, route_json
from test_materials import item, prepare  # noqa: F401
from test_runtime import BlockingProvider
from test_sessions_memory import append  # noqa: F401

from reading_agent.agents.models import RouteFinal
from reading_agent.infrastructure.locking import FileLock
from reading_agent.services.discussion import Discussion
from reading_agent.storage.database import Conflict, NotFound


async def test_startup_marks_only_interrupted_material_stages(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    tm = service.commit_route(
        mid, RouteFinal.model_validate_json(route_json([item(tid, tag, needs_deep_read=True)]))
    )[0]
    with service.store.transaction() as db:
        db.execute("UPDATE materials SET routing_status='running' WHERE id=?", (mid,))
    service.store.interrupt()
    assert (
        service.store.one("SELECT * FROM materials WHERE id=?", (mid,))["routing_status"]
        == "failed"
    )
    assert (
        service.store.one("SELECT * FROM topic_materials WHERE id=?", (tm,))["deep_summary_status"]
        == "failed"
    )
    assert service.provider.requests == []


async def test_delete_then_reimport_cannot_receive_old_deep_result(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    final = RouteFinal.model_validate_json(route_json([item(tid, tag)]))
    old = service.commit_route(mid, final)[0]
    service.store.delete("topic_materials", old)
    new = service.commit_route(mid, final)[0]
    assert old != new
    with pytest.raises(NotFound):
        await service.deep_read(old)
    assert (
        service.store.one("SELECT * FROM topic_materials WHERE id=?", (new,))["deep_summary"]
        is None
    )


async def test_explicit_import_failure_has_no_empty_relationship(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    service.provider = ScriptedProvider(route_json(status="failed"))
    result = await service.route(mid, target=tid)
    assert result["status"] == "failed" and service.store.inbox(tid) == []
    assert (
        service.store.one("SELECT * FROM materials WHERE id=?", (mid,))["routing_status"]
        == "pending"
    )


async def test_selected_and_model_initialization_are_one_shot(setup):
    from test_materials import Feeds

    from reading_agent.services.materials import MaterialService

    store, tid, tag = setup
    service = MaterialService(
        store, Feeds(), ScriptedProvider('{"selected_keys":["one"]}', route_json([item(tid, tag)]))
    )
    source = await service.add_source("rss", "https://example.test/model", [tid], policy="model")
    assert len(source["saved"]) == 1 and store.inbox(tid)
    assert (await service.fetch(source["id"]))["saved"] == []
    empty = await service.add_source(
        "skip", "https://example.test/selected", policy="selected", selected_keys=[]
    )
    assert empty["saved"] == []
    # 未选择的历史不会在日常更新中被重新导入，只能显式勾选。
    assert (await service.fetch(empty["id"]))["saved"] == []
    assert len((await service.import_history(empty["id"], ["one"]))["saved"]) == 1


def test_cross_process_lock_is_exclusive(tmp_path):
    with FileLock(tmp_path / "source.lock"):
        with pytest.raises(Conflict):
            with FileLock(tmp_path / "source.lock"):
                pass
    with FileLock(tmp_path / "source.lock"):
        pass


async def test_stop_persists_partial_and_does_not_erase_user_question(state):
    store, service, sessions, memory, created, tid, tm = state
    provider = BlockingProvider()
    service.provider = provider
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    discussion.start(bid, "保留我的问题", expected_leaf=sessions.path(bid)[-1]["id"])
    await asyncio.wait_for(provider.started.wait(), 1)
    await discussion.stop(bid)
    path = sessions.path(bid)
    assert path[-2]["data"]["content"] == "保留我的问题"
    assert path[-1]["data"]["incomplete"] is True
    assert discussion.runs[bid].phase == "stopped" and provider.closed.is_set()


async def test_deleted_discussion_cannot_be_resurrected(state):
    store, service, sessions, memory, created, tid, tm = state
    provider = BlockingProvider()
    service.provider = provider
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    discussion.start(bid, "问题", expected_leaf=sessions.path(bid)[-1]["id"])
    await asyncio.wait_for(provider.started.wait(), 1)
    store.delete("topic_materials", tm)
    await discussion.stop(bid)
    assert store.all("SELECT * FROM sessions") == []
    assert store.all("SELECT * FROM entries") == []
    assert discussion.runs[bid].phase == "stopped"


async def test_compression_is_scoped_and_keeps_original_entries(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    for i in range(5):
        append(sessions, bid, f"问题{i}；保留边界")
        append(sessions, bid, f"答案{i}", "assistant")
    fork = sessions.fork(bid, sessions.path(bid)[-1]["id"])
    append(sessions, fork, "其他分支独占内容")
    before = len(store.all("SELECT * FROM entries"))
    service.provider = ScriptedProvider(
        '{"summary":"已讨论问题0到2，保留边界和未完成项。","retained_constraints":["保留边界"]}'
    )
    discussion = Discussion(store, sessions, service)
    await discussion.compact(bid)
    assert len(store.all("SELECT * FROM entries")) == before + 1
    root, history = sessions.context(bid)
    assert "其他分支独占内容" not in str(history)
    assert "问题4" in str(history) and "保留边界" in str(history)
    payload = service.provider.requests[0].messages[-1]["content"]
    assert "其他分支独占内容" not in payload
    assert sessions.path(bid)[-1]["type"] == "compaction"


async def test_material_deletion_cancels_inflight_deep_read(setup):
    service, sid, mid, tid, tag = await prepare(setup)
    tm = service.commit_route(mid, RouteFinal.model_validate_json(route_json([item(tid, tag)])))[0]
    provider = BlockingProvider()
    service.provider = provider
    pending = asyncio.create_task(service.deep_read(tm))
    await asyncio.wait_for(provider.started.wait(), 1)
    service.cancel_targets("topic_materials", tm)
    service.store.delete("topic_materials", tm)
    result = await asyncio.wait_for(pending, 1)
    assert result["status"] == "cancelled" and provider.closed.is_set()
    assert service.store.all("SELECT * FROM topic_materials WHERE id=?", (tm,)) == []
