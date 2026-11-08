import asyncio

import httpx
from conftest import ScriptedProvider
from desktop_fixture import seed
from test_materials import Feeds

from reading_agent.interfaces.api import create_app


async def test_api_authorization_storage_jobs_and_deletion(tmp_path):
    app = create_app(
        directory=tmp_path, token="test-token", provider=ScriptedProvider(), fetcher=Feeds()
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/api/state")).status_code == 401
            client.headers["authorization"] = "Bearer test-token"
            tid = (await client.post("/api/topics", json={"name": "学习"})).json()["id"]
            tag = (await client.post(f"/api/topics/{tid}/tags", json={"name": "工具"})).json()["id"]
            assert tag
            source = (
                await client.post(
                    "/api/sources",
                    json={"name": "rss", "url": "https://example.test/feed", "topic_ids": [tid]},
                )
            ).json()
            for _ in range(20):
                job = (await client.get("/api/jobs/" + source["job_id"])).json()
                if job["status"] != "running":
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "completed"
            mid = (
                await client.post("/api/memories", json={"content": "我偏好简短", "topic_id": tid})
            ).json()["id"]
            assert (await client.get("/api/memories", params={"topic_id": tid})).json()[0][
                "id"
            ] == mid
            assert (
                await client.patch("/api/memories/" + mid, json={"content": "我偏好详细"})
            ).status_code == 200
            assert (await client.delete("/api/objects/topics/" + tid)).status_code == 200
            assert (await client.get("/api/memories", params={"topic_id": tid})).json() == []
            assert (await client.delete("/api/objects/topics/" + tid)).status_code == 404


async def test_api_rejects_invalid_inputs(tmp_path):
    app = create_app(directory=tmp_path, token="t", provider=ScriptedProvider(), fetcher=Feeds())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local",
            headers={"authorization": "Bearer t"},
        ) as client:
            assert (await client.post("/api/topics", json={"name": " "})).status_code == 400
            assert (
                await client.post("/api/topics", json={"name": "x", "extra": True})
            ).status_code == 422
            assert (await client.post("/api/topics", content="x" * 65537)).status_code == 413


async def test_topic_discussions_are_authenticated_isolated_and_follow_deletion(tmp_path):
    await asyncio.to_thread(seed, str(tmp_path), observability=True)
    app = create_app(directory=tmp_path, token="workspace-token", provider=ScriptedProvider())
    store = app.state.store
    topic = store.one("SELECT id FROM topics")["id"]
    other = store.topic("另一个Topic")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get(f"/api/topics/{topic}/discussions")).status_code == 401
            client.headers["authorization"] = "Bearer workspace-token"
            rows = (await client.get(f"/api/topics/{topic}/discussions")).json()
            assert len(rows) == 1 and rows[0]["topic_id"] == topic
            assert "material_title" in rows[0] and "model_input" not in rows[0]
            assert (await client.get(f"/api/topics/{other}/discussions")).json() == []
            assert (
                await client.get(f"/api/topics/{topic}/discussions?limit=51")
            ).status_code == 422
            assert (await client.get("/api/topics/missing/discussions")).status_code == 404
            assert (await client.delete(f"/api/branches/{rows[0]['id']}")).status_code == 200
            assert (await client.get(f"/api/topics/{topic}/discussions")).json() == []
