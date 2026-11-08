import asyncio
import json

import httpx
import pytest
from conftest import ScriptedProvider
from test_materials import item, prepare

from reading_agent.agents.models import RouteFinal
from reading_agent.agents.provider import ProviderEvent, ToolCall, Turn
from reading_agent.interfaces.api import create_app


def test_workspace_notifications_only_after_successful_business_commit(setup):
    store, *_ = setup
    seen = []
    store.change_listeners.add(seen.append)
    revision = store.revision
    store.all("SELECT * FROM topics")
    assert seen == []
    with pytest.raises(ValueError):
        with store.transaction() as db:
            db.execute("DELETE FROM tags")
            raise ValueError("rollback")
    assert seen == [] and store.revision == revision
    store.topic("new topic")
    assert seen == [revision + 1]
    store.change_listeners.discard(seen.append)


async def test_workspace_sse_snapshot_coalescing_and_cleanup(tmp_path):
    app = create_app(directory=tmp_path, token="test", provider=ScriptedProvider())
    store = app.state.store
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/workspace/events")

    class Request:
        async def is_disconnected(self):
            return False

    response = await endpoint(Request())
    stream = response.body_iterator
    async with asyncio.timeout(3):
        first = json.loads((await anext(stream)).removeprefix("data: ").strip())
        assert first["type"] == "workspace_changed"
        assert len(store.change_listeners) == 1
        for index in range(25):
            store.topic(f"private topic {index}")
        await asyncio.sleep(0)
        event = json.loads((await anext(stream)).removeprefix("data: ").strip())
        assert event == {"type": "workspace_changed", "revision": store.revision}
        assert event["revision"] > first["revision"]
        await stream.aclose()
    assert not store.change_listeners
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (await client.get("/api/workspace/events")).status_code == 401


async def prepare_deep(setup):
    from conftest import route_json

    service, _, mid, tid, tag = await prepare(setup)
    final = RouteFinal.model_validate_json(route_json([item(tid, tag, needs_deep_read=True)]))
    tm = service.commit_route(mid, final)[0]
    return service, mid, tm


async def test_cancel_deep_read_closes_model_request_and_does_not_save_summary(setup):
    service, _, tm = await prepare_deep(setup)
    started, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        async def stream(self, request):
            try:
                started.set()
                await asyncio.Event().wait()
                yield ProviderEvent("turn", turn=Turn(content="unreachable"))
            finally:
                closed.set()

    service.provider = Provider()
    work = asyncio.create_task(service.deep_read(tm))
    async with asyncio.timeout(3):
        await started.wait()
        assert (await service.cancel_deep_read(tm))["status"] == "cancelled"
        assert closed.is_set()  # API returns only after provider cleanup.
        assert (await work)["status"] == "cancelled"
    row = service.store.one("SELECT * FROM topic_materials WHERE id=?", (tm,))
    assert row["deep_summary_status"] == "cancelled" and not row["deep_summary"]
    assert not row["deep_summary_error"]
    assert not service.deep_finished and not service.cancels and not service.active


async def test_cancel_deep_read_during_body_fetch_and_manual_retry(setup):
    service, mid, tm = await prepare_deep(setup)
    started, closed = asyncio.Event(), asyncio.Event()

    async def body(url):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    service.fetcher.body = body
    service.provider = ScriptedProvider(
        Turn(
            calls=[
                ToolCall(
                    "read",
                    "material_read",
                    json.dumps({"material_id": mid, "start": 0, "length": 100}),
                )
            ],
            finish_reason="tool_calls",
        )
    )
    work = asyncio.create_task(service.deep_read(tm))
    async with asyncio.timeout(3):
        await started.wait()
        await service.cancel_deep_read(tm)
        assert (await work)["status"] == "cancelled" and closed.is_set()
    # A new manual run is allowed after cancellation; an invalid answer still fails normally.
    service.provider = ScriptedProvider("{}", "{}")
    assert (await service.deep_read(tm))["status"] == "failed"
    assert (
        service.store.one("SELECT * FROM topic_materials WHERE id=?", (tm,))["deep_summary_status"]
        == "failed"
    )


async def test_cancel_pending_skips_automatic_but_preserves_completed(setup):
    service, _, tm = await prepare_deep(setup)
    assert (await service.cancel_deep_read(tm))["status"] == "cancelled"
    assert (await service.deep_read(tm, automatic=True))["status"] == "cancelled"
    assert not service.provider.requests
    with service.store.transaction() as db:
        db.execute(
            "UPDATE topic_materials SET deep_summary='existing',"
            "deep_summary_status='succeeded' WHERE id=?",
            (tm,),
        )
    assert (await service.cancel_deep_read(tm))["status"] == "succeeded"
    assert (
        service.store.one("SELECT * FROM topic_materials WHERE id=?", (tm,))["deep_summary"]
        == "existing"
    )


@pytest.mark.parametrize(
    "recommended,tagged,needs,status",
    [
        (True, True, True, "pending"),
        (False, True, True, "not_scheduled"),
        (True, False, True, "not_scheduled"),
        (True, True, False, "not_scheduled"),
    ],
)
async def test_automatic_deep_read_conditions(setup, recommended, tagged, needs, status):
    from conftest import route_json

    service, _, mid, tid, tag = await prepare(setup)
    final = RouteFinal.model_validate_json(
        route_json(
            [
                item(
                    tid,
                    tag,
                    is_recommended=recommended,
                    tag_ids=[tag] if tagged else [],
                    needs_deep_read=needs,
                )
            ]
        )
    )
    tm = service.commit_route(mid, final)[0]
    row = service.store.one("SELECT * FROM topic_materials WHERE id=?", (tm,))
    assert row["deep_summary_status"] == status
    assert bool(service.store.inbox(tid, recommended=True)) == (recommended and tagged)
    service.store.feedback(tm, interest="uninterested")
    assert not service.store.inbox(tid, recommended=True)


async def test_cancel_deep_read_api_waits_for_cleanup(tmp_path):
    from conftest import route_json
    from test_materials import Feeds

    started, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        async def stream(self, request):
            try:
                started.set()
                await asyncio.Event().wait()
                yield ProviderEvent("turn", turn=Turn(content="unreachable"))
            finally:
                closed.set()

    app = create_app(directory=tmp_path, token="test", provider=Provider(), fetcher=Feeds())
    async with app.router.lifespan_context(app):
        store, service = app.state.store, app.state.service
        tid = store.topic("test")
        tag = store.tag(tid, "tag")
        sid = (await service.add_source("feed", "https://example.test/rss", [tid]))["id"]
        mid = service.ingest(sid, await service.fetcher.feed(""))[0]
        tm = service.commit_route(
            mid, RouteFinal.model_validate_json(route_json([item(tid, tag, needs_deep_read=True)]))
        )[0]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            path = f"/api/topic-materials/{tm}/deep-read"
            assert (await client.post(path + "/cancel")).status_code == 401
            client.headers["authorization"] = "Bearer test"
            assert (
                await client.post("/api/topic-materials/missing/deep-read/cancel")
            ).status_code == 404
            job = (await client.post(path, json={})).json()
            async with asyncio.timeout(3):
                await started.wait()
                response = await client.post(path + "/cancel")
            assert response.json()["status"] == "cancelled" and closed.is_set()
            status = (await client.get("/api/jobs/" + job["job_id"])).json()
            assert status["status"] == "completed" and status["result"]["status"] == "cancelled"


async def test_deep_subscription_snapshot_cancel_and_disconnect(setup):
    service, _, tm = await prepare_deep(setup)
    started = asyncio.Event()

    class Provider:
        async def stream(self, request):
            yield ProviderEvent("text", text='{"deep_summary":"实时首段')
            started.set()
            await asyncio.Event().wait()

    service.provider = Provider()
    work = asyncio.create_task(service.deep_read(tm))
    async with asyncio.timeout(3):
        await started.wait()
        run, queue, snapshot = service.subscribe_deep(tm)
        assert snapshot["answer_draft"] == "实时首段"
        # Closing a view unsubscribes; the independent task remains active.
        run.subscribers.discard(queue)
        assert not run.cancel.is_set() and not work.done()
        run, queue, replay = service.subscribe_deep(tm)
        assert replay["answer_draft"] == "实时首段"
        await service.cancel_deep_read(tm)
        await work
        assert run.phase == "stopped"
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert events[-1]["type"] == "status" and events[-1]["phase"] == "stopped"
        run.subscribers.discard(queue)


async def test_deep_read_stream_tools_and_valid_final_are_saved(setup):
    service, mid, tm = await prepare_deep(setup)
    body = await service.fetcher.body("")
    service.provider = ScriptedProvider(
        Turn(
            calls=[
                ToolCall(
                    "read",
                    "material_read",
                    json.dumps({"material_id": mid, "start": 0, "length": 1000}),
                )
            ],
            finish_reason="tool_calls",
        ),
        json.dumps(
            {
                "status": "succeeded",
                "reason": "完成",
                "deep_summary": "合法精读结果",
                "coverage": [{"material_id": mid, "start": 0, "end": len(body)}],
                "limitations": "",
            }
        ),
    )
    result = await service.deep_read(tm)
    assert result["status"] == "succeeded"
    run = service.deep_runs[tm]
    assert run.phase == "completed" and run.answer_draft == "合法精读结果"
    assert run.activities[0]["name"] == "material_read" and run.activities[0]["status"] != "running"
    assert (
        service.store.one("SELECT deep_summary FROM topic_materials WHERE id=?", (tm,))[
            "deep_summary"
        ]
        == "合法精读结果"
    )
