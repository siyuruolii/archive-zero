from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import ScriptedProvider

from reading_agent.infrastructure.fetching import FeedItem
from reading_agent.interfaces.api import create_app
from reading_agent.services.materials import MaterialService
from reading_agent.storage.database import Conflict


class TimelineFeed:
    async def feed(self, url):
        dates = {
            "old": "2025-01-01",
            "equal": "2026-01-01T00:00:00Z",
            "offset-old": "2026-01-01T07:00:00+08:00",
            "new": "2026-01-01T08:00:01+08:00",
            "rss-new": "Thu, 01 Jan 2026 00:00:02 GMT",
            "unknown": None,
            "bad": "not-a-date",
        }
        return [
            FeedItem(key, key, "https://example.test/" + key, "", date)
            for key, date in dates.items()
        ]


async def prepare_source(store):
    service = MaterialService(store, TimelineFeed(), ScriptedProvider())
    service.route = AsyncMock(return_value={"status": "succeeded"})
    sid = (await service.add_source("feed", "https://example.test/rss"))["id"]
    with store.transaction() as db:
        db.execute("UPDATE sources SET created_at=? WHERE id=?", ("2026-01-01T00:00:00+00:00", sid))
    return service, sid


async def test_updates_use_subscription_date_and_survive_restart(setup):
    store, *_ = setup
    service, sid = await prepare_source(store)
    result = await service.fetch(sid)
    assert len(result["saved"]) == 2 and result["skipped_by_date"] == 5
    assert {row["item_key"] for row in store.materials()} == {"new", "rss-new"}
    assert service.route.await_count == 2
    service = MaterialService(store, TimelineFeed(), ScriptedProvider())
    service.route = AsyncMock()
    assert (await service.fetch(sid))["saved"] == []
    service.route.assert_not_awaited()
    assert len(await service.history(sid)) == 5


async def test_history_is_opt_in_validated_and_deduplicated(setup):
    store, *_ = setup
    service, sid = await prepare_source(store)
    assert len(await service.history(sid)) == 7
    assert store.materials() == []  # Preview has no write or model call.
    service.route.assert_not_awaited()
    with pytest.raises(ValueError, match="outside current feed"):
        await service.import_history(sid, ["foreign-key"])
    assert not store.materials()
    assert len((await service.import_history(sid, ["old", "unknown"]))["saved"]) == 2
    assert (await service.import_history(sid, ["old", "old"]))["saved"] == []
    assert {r["item_key"] for r in await service.history(sid)} == {
        "equal",
        "offset-old",
        "new",
        "rss-new",
        "bad",
    }
    await service.fetch(sid)
    assert {r["item_key"] for r in store.materials()} == {"old", "unknown", "new", "rss-new"}
    service.source_state(sid, enabled=False)
    for operation in (
        service.fetch(sid),
        service.history(sid),
        service.import_history(sid, ["bad"]),
    ):
        with pytest.raises(Conflict):
            await operation


async def test_source_history_api_auth_and_job(tmp_path):
    app = create_app(
        directory=tmp_path, token="test", provider=ScriptedProvider(), fetcher=TimelineFeed()
    )
    async with app.router.lifespan_context(app):
        service = app.state.service
        sid = (await service.add_source("feed", "https://example.test/rss"))["id"]
        service.route = AsyncMock(return_value={"status": "succeeded"})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get(f"/api/sources/{sid}/history")).status_code == 401
            client.headers["authorization"] = "Bearer test"
            assert len((await client.get(f"/api/sources/{sid}/history")).json()) == 7
            job = (
                await client.post(f"/api/sources/{sid}/history", json={"selected_keys": ["old"]})
            ).json()
            import asyncio

            for _ in range(30):
                state = (await client.get("/api/jobs/" + job["job_id"])).json()
                if state["status"] != "running":
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == "completed"
            assert len(state["result"]["saved"]) == 1
            assert len((await client.get(f"/api/sources/{sid}/history")).json()) == 6
