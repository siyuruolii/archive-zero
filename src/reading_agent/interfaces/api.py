"""仅绑定本机的应用接口；桌面随机令牌授权，后台任务不归连接所有。"""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from reading_agent.agents.model_router import ModelCatalog, ModelRouter
from reading_agent.agents.provider import ChatProvider, ProviderError
from reading_agent.infrastructure.configuration import (
    catalog_from_input,
    data_dir,
    load_catalog,
    memory_policy,
    memory_provider,
    public_catalog,
    save_catalog,
)
from reading_agent.infrastructure.fetching import Fetcher
from reading_agent.infrastructure.locking import FileLock
from reading_agent.services.approvals import CommandApprovals
from reading_agent.services.discussion import Discussion
from reading_agent.services.materials import MaterialService
from reading_agent.services.memory import Memory
from reading_agent.services.sessions import Sessions
from reading_agent.storage.database import Conflict, NotFound, Store, identity


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    url: str = Field(default="", max_length=2000)
    topic_id: str | None = None
    topic_ids: list[str] = Field(default_factory=list, max_length=20)
    policy: str = "none"
    selected_keys: list[str] = Field(default_factory=list, max_length=200)
    enabled: bool | None = None
    linked: bool = True
    removed: bool = False
    is_read: bool | None = None
    interest: str | None = None
    tag_ids: list[str] | None = None
    question: str = Field(default="", max_length=8000)
    expected_leaf: str = ""
    expected_run_id: str = ""
    entry_id: str = ""
    content: str = Field(default="", max_length=2000)
    kind: str = "profile"
    approved: bool = False


def run_event_response(request, run, queue, snapshot):
    async def generate():
        try:
            yield (
                "data: "
                + json.dumps({"type": "snapshot", "data": snapshot}, ensure_ascii=False)
                + "\n\n"
            )
            if not run or not snapshot or snapshot["phase"] != "running":
                return
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    if (
                        event["type"] == "status" and event.get("phase") not in {None, "running"}
                    ) or (
                        event["type"] == "snapshot"
                        and event.get("data", {}).get("phase") not in {None, "running"}
                    ):
                        return
                except TimeoutError:
                    if run.phase != "running":
                        yield (
                            "data: "
                            + json.dumps(
                                {"type": "snapshot", "data": run.snapshot()}, ensure_ascii=False
                            )
                            + "\n\n"
                        )
                        return
                    yield ": heartbeat\n\n"
        finally:
            if run:
                run.subscribers.discard(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class Jobs:
    def __init__(self):
        self.items = {}
        self.tasks = {}

    def start(self, action):
        jid = identity()
        self.items[jid] = {"id": jid, "status": "running"}

        async def work():
            try:
                self.items[jid] = {"id": jid, "status": "completed", "result": await action()}
            except Exception as exc:
                self.items[jid] = {"id": jid, "status": "failed", "error": type(exc).__name__}
            finally:
                self.tasks.pop(jid, None)

        self.tasks[jid] = asyncio.create_task(work())
        # 仅清理已结束的临时元数据，不删除业务数据，不建立持久任务恢复平台。
        if len(self.items) > 128:
            for old in list(self.items):
                if old not in self.tasks and old != jid:
                    self.items.pop(old)
                    break
        return {"job_id": jid}

    async def shutdown(self):
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class UnconfiguredProvider:
    model_name = "尚未配置模型"

    async def stream(self, request):
        raise ProviderError("credentials_missing")
        yield  # pragma: no cover


def create_app(*, directory=None, token=None, provider=None, fetcher=None):
    directory = Path(directory or data_dir())
    token = token or secrets.token_urlsafe(32)
    store = Store(directory / "reading.db")
    sessions = Sessions(store)
    jobs = Jobs()
    client = httpx.AsyncClient(follow_redirects=False, trust_env=False, max_redirects=5)
    if provider is None:
        try:
            provider = ModelRouter(load_catalog(directory), client)
        except (OSError, ValueError):
            provider = UnconfiguredProvider()
    service = MaterialService(store, fetcher or Fetcher(client), provider)
    approvals = CommandApprovals(store.publish_change)
    service.command_approvals = approvals
    memory = Memory(
        store,
        provider,
        extraction_provider=memory_provider(provider, "extract"),
        consolidation_provider=memory_provider(provider, "consolidate"),
        consolidation_policy=memory_policy(),
    )
    service.memory = memory
    discussion = Discussion(store, sessions, service, memory)

    def active_catalog():
        if isinstance(provider, ModelRouter):
            return provider.catalog
        if isinstance(provider, ChatProvider):
            return ModelCatalog(default="default", profiles={"default": provider.settings})
        return None

    @asynccontextmanager
    async def lifespan(app):
        with FileLock(directory / "backend.lock"):
            store.interrupt()
            sessions.interrupt_runs()

            async def poll_sources():
                while True:
                    await asyncio.sleep(300)
                    for source in store.all(
                        "SELECT id FROM sources WHERE is_enabled=1 AND is_removed=0"
                    ):
                        jobs.start(lambda sid=source["id"]: service.fetch(sid))

            polling = asyncio.create_task(poll_sources())

            async def poll_memory():
                while True:
                    try:
                        memory.maybe_consolidate()
                    except Exception as exc:
                        # 调度错误与讨论/来源检查分离，不回显资料或供应商响应。
                        app.state.memory_scheduler_error = type(exc).__name__
                    await asyncio.sleep(60)

            memory_polling = asyncio.create_task(poll_memory())
            try:
                yield
            finally:
                polling.cancel()
                memory_polling.cancel()
                await asyncio.gather(polling, memory_polling, return_exceptions=True)
                await jobs.shutdown()
                await discussion.shutdown()
                await memory.shutdown()
                await client.aclose()

    app = FastAPI(
        title="Archive Zero", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.store, app.state.service, app.state.sessions = store, service, sessions
    app.state.memory, app.state.discussion, app.state.jobs = memory, discussion, jobs
    app.state.token = token
    app.state.approvals = approvals

    @app.get("/api/approvals")
    async def pending_approvals():
        return approvals.list()

    @app.post("/api/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, body: Input):
        approvals.resolve(approval_id, body.approved)
        return {"status": "resolved"}

    @app.get("/api/settings/models")
    async def model_settings():
        return public_catalog(active_catalog())

    @app.get("/api/settings/model-usage")
    async def model_usage():
        providers = (
            provider.providers if isinstance(provider, ModelRouter) else {"default": provider}
        )
        result = {}
        for name, item in providers.items():
            if hasattr(item, "statistics"):
                stats = dict(item.statistics)
                for field in ("hit", "miss"):
                    if not stats["cache_" + field + "_reported_calls"]:
                        stats["cache_" + field + "_tokens"] = None
                result[name] = stats
        return {"profiles": result, "scope": "current_process_and_configuration"}

    @app.put("/api/settings/models")
    async def update_model_settings(body: dict):
        nonlocal provider
        catalog = catalog_from_input(body, active_catalog())
        replacement = ModelRouter(catalog, client)
        save_catalog(catalog, directory)
        provider = replacement
        service.provider = replacement
        memory.provider = replacement
        memory.extraction_provider = None
        memory.consolidator.provider = None
        return public_catalog(catalog)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            credential = request.headers.get("authorization", "")
            if not secrets.compare_digest(credential, "Bearer " + token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            if (
                request.headers.get("content-length", "").isdigit()
                and int(request.headers["content-length"]) > 65536
            ):
                return JSONResponse({"error": "request too large"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(NotFound)
    async def missing(request, error):
        return JSONResponse({"error": "目标已不存在，请刷新"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"error": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"error": "输入或结果不符合约束：" + str(error)[:160]}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def malformed_input(request, error):
        # 默认422会回显输入，模型设置可能含密钥。
        return JSONResponse({"error": "请求格式不符合约束"}, status_code=422)

    @app.post("/api/shutdown")
    async def shutdown_backend():
        callback = getattr(app.state, "request_shutdown", None)
        if callback:
            callback()
        return {"status": "stopping"}

    @app.get("/api/state")
    async def state():
        return {
            "topics": store.all("SELECT * FROM topics ORDER BY created_at"),
            "tags": store.all("SELECT * FROM tags"),
            "sources": store.all("SELECT * FROM sources WHERE is_removed=0"),
            "links": store.all("SELECT * FROM topic_sources"),
            "model": getattr(provider, "model_name", "test"),
            "memory_error": memory.last_extraction_error,
        }

    @app.post("/api/topics")
    async def add_topic(body: Input):
        return {"id": store.topic(body.name, body.description)}

    @app.post("/api/topics/{tid}/tags")
    async def add_tag(tid: str, body: Input):
        return {"id": store.tag(tid, body.name, body.description)}

    @app.patch("/api/topics/{tid}")
    async def update_topic(tid: str, body: Input):
        if not body.name.strip():
            raise ValueError("name required")
        with store.transaction() as db:
            store.require(db, "topics", tid)
            db.execute(
                "UPDATE topics SET name=?,description=? WHERE id=?",
                (body.name, body.description, tid),
            )
        return {"status": "saved"}

    @app.post("/api/sources/preview")
    async def preview(body: Input):
        from dataclasses import asdict

        return [asdict(i) for i in await service.fetcher.feed(body.url)]

    @app.post("/api/sources")
    async def add_source(body: Input):
        return jobs.start(
            lambda: service.add_source(
                body.name, body.url, body.topic_ids, body.policy, body.selected_keys
            )
        )

    @app.patch("/api/sources/{sid}")
    async def source_state(sid: str, body: Input):
        service.source_state(sid, enabled=body.enabled, removed=body.removed)
        return {"status": "saved"}

    @app.put("/api/sources/{sid}/topics/{tid}")
    async def link(sid: str, tid: str, body: Input):
        store.link(tid, sid, body.linked)
        return {"status": "saved"}

    @app.post("/api/sources/{sid}/fetch")
    async def fetch(sid: str):
        return jobs.start(lambda: service.fetch(sid))

    @app.get("/api/sources/{sid}/history")
    async def source_history(sid: str):
        return await service.history(sid)

    @app.post("/api/sources/{sid}/history")
    async def import_source_history(sid: str, body: Input):
        return jobs.start(lambda: service.import_history(sid, body.selected_keys))

    @app.get("/api/jobs/{jid}")
    async def job(jid: str):
        if jid not in jobs.items:
            raise HTTPException(404, "job not found; interrupted jobs are not replayed")
        return jobs.items[jid]

    @app.get("/api/materials")
    async def materials():
        return store.materials()

    @app.post("/api/materials/{mid}/route")
    async def route(mid: str):
        return jobs.start(lambda: service.route(mid))

    @app.post("/api/materials/{mid}/import")
    async def import_material(mid: str, body: Input):
        if not body.topic_id:
            raise ValueError("target Topic required")
        return jobs.start(lambda: service.route(mid, target=body.topic_id))

    @app.get("/api/topics/{tid}/inbox")
    async def inbox(tid: str, recommended: bool = False, tags: str = "", untagged: bool = False):
        return store.inbox(
            tid, recommended=recommended, tags=tags.split(",") if tags else (), untagged=untagged
        )

    @app.patch("/api/topic-materials/{tm_id}")
    async def feedback(tm_id: str, body: Input):
        store.feedback(tm_id, is_read=body.is_read, interest=body.interest, tag_ids=body.tag_ids)
        if body.is_read is True:
            memory.experience(tm_id, "read")
        if body.interest in {"interested", "uninterested"}:
            memory.experience(tm_id, body.interest)
        return {"status": "saved"}

    @app.post("/api/topic-materials/{tm_id}/deep-read")
    async def deep_read(tm_id: str, body: Input):
        return jobs.start(lambda: service.deep_read(tm_id, question=body.question))

    @app.get("/api/topic-materials/{tm_id}/deep-read/events")
    async def deep_read_events(tm_id: str, request: Request):
        run, queue, snapshot = service.subscribe_deep(tm_id)
        return run_event_response(request, run, queue, snapshot)

    @app.post("/api/topic-materials/{tm_id}/deep-read/cancel")
    async def cancel_deep_read(tm_id: str):
        return await service.cancel_deep_read(tm_id)

    @app.get("/api/workspace/events")
    async def workspace_events(request: Request):
        async def generate():
            queue = asyncio.Queue(maxsize=1)
            loop = asyncio.get_running_loop()

            def enqueue(revision):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(revision)

            def changed(revision):
                loop.call_soon_threadsafe(enqueue, revision)

            store.change_listeners.add(changed)
            try:
                yield (
                    "data: "
                    + json.dumps({"type": "workspace_changed", "revision": store.revision})
                    + "\n\n"
                )
                while not await request.is_disconnected():
                    try:
                        revision = await asyncio.wait_for(queue.get(), timeout=15)
                        yield (
                            "data: "
                            + json.dumps({"type": "workspace_changed", "revision": revision})
                            + "\n\n"
                        )
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                store.change_listeners.discard(changed)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete("/api/objects/{table}/{item_id}")
    async def delete(table: str, item_id: str):
        service.cancel_targets(table, item_id)
        for bid, run in list(discussion.runs.items()):
            if run.phase != "running":
                continue
            rows = store.all(
                """SELECT s.id session_id,tm.id tm_id,tm.topic_id
                FROM branches b JOIN sessions s ON s.id=b.session_id
                JOIN topic_materials tm ON tm.id=s.topic_material_id WHERE b.id=?""",
                (bid,),
            )
            if rows and (
                (table == "sessions" and rows[0]["session_id"] == item_id)
                or (table == "topics" and rows[0]["topic_id"] == item_id)
                or (table == "topic_materials" and rows[0]["tm_id"] == item_id)
            ):
                run.cancel.set()
        store.delete(table, item_id)
        sessions.flush_deleted()
        # 在途任务仍按原 ID 保存，删除保护是事务检查而非仅靠取消信号。
        return {"status": "deleted"}

    @app.get("/api/topic-materials/{tm_id}/sessions")
    async def list_sessions(tm_id: str):
        return store.all(
            """SELECT s.id session_id,b.* FROM sessions s JOIN branches b ON b.session_id=s.id
                    WHERE s.topic_material_id=? ORDER BY b.created_at""",
            (tm_id,),
        )

    @app.get("/api/topics/{tid}/discussions")
    async def topic_discussions(tid: str, limit: int = Query(default=30, ge=1, le=50)):
        with store.transaction() as db:
            store.require(db, "topics", tid)
        return store.all(
            "SELECT b.id,b.session_id,b.title branch_title,s.topic_material_id,tm.topic_id,"
            "m.title material_title,e.created_at updated_at "
            "FROM branches b JOIN sessions s ON s.id=b.session_id "
            "JOIN topic_materials tm ON tm.id=s.topic_material_id "
            "JOIN materials m ON m.id=tm.material_id JOIN entries e ON e.id=b.leaf_entry_id "
            "WHERE tm.topic_id=? ORDER BY e.created_at DESC,b.id LIMIT ?",
            (tid, limit),
        )

    @app.post("/api/topic-materials/{tm_id}/sessions")
    async def create_session(tm_id: str):
        return sessions.create(tm_id)

    @app.get("/api/branches/{bid}")
    async def branch(bid: str):
        path = sessions.path(bid)
        run = discussion.runs.get(bid)
        return {
            "entries": path,
            "leaf_id": path[-1]["id"],
            "run": run.snapshot() if run else None,
            "last_run": sessions.last_run(bid),
            "recovery": discussion.recovery_info(bid),
        }

    @app.get("/api/sessions/{sid}/events")
    async def session_events(
        sid: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
        run_id: str | None = None,
    ):
        return sessions.events(sid, after=after, limit=limit, run_id=run_id)

    @app.post("/api/branches/{bid}/messages")
    async def message(bid: str, body: Input):
        return {"run_id": discussion.start(bid, body.question, expected_leaf=body.expected_leaf)}

    @app.post("/api/branches/{bid}/stop")
    async def stop(bid: str):
        await discussion.stop(bid)
        return {"status": "stopped"}

    @app.post("/api/branches/{bid}/resume")
    async def resume_branch(bid: str, body: Input):
        return {"run_id": discussion.resume(bid, body.expected_run_id)}

    @app.post("/api/branches/{bid}/fork")
    async def fork(bid: str, body: Input):
        return {"id": sessions.fork(bid, body.entry_id, body.name or "分支讨论")}

    @app.post("/api/branches/{bid}/compact")
    async def compact(bid: str):
        run = discussion.runs.get(bid)
        if run and run.phase == "running":
            raise Conflict("stop current discussion before compression")
        return {"leaf_id": await discussion.compact(bid)}

    @app.delete("/api/branches/{bid}")
    async def delete_branch(bid: str):
        await discussion.stop(bid)
        sessions.delete_branch(bid)
        return {"status": "deleted"}

    @app.get("/api/branches/{bid}/events")
    async def events(bid: str, request: Request):
        sessions.path(bid)
        run, queue, snapshot = discussion.subscribe(bid)

        return run_event_response(request, run, queue, snapshot)

    @app.get("/api/memories")
    async def memories(topic_id: str | None = None, category: str | None = None):
        return memory.list(topic_id, category=category, limit=20)

    @app.get("/api/memory/consolidation")
    async def consolidation_status():
        return {
            **memory.consolidator.status(),
            "scheduler_error": getattr(app.state, "memory_scheduler_error", None),
        }

    @app.get("/api/session-memory/search")
    async def session_memory_search(
        query: str = Query(default="", max_length=200),
        topic_id: str | None = None,
        limit: int = Query(default=3, ge=1, le=5),
    ):
        # 浏览接口只检索片段；模型提炼由 Agent 工具按需触发并计入 Loop 预算。
        return memory.search_sessions(query, topic_id=topic_id, limit=limit)

    @app.post("/api/memories")
    async def add_memory(body: Input):
        return {"id": memory.add(body.content, topic_id=body.topic_id)}

    @app.patch("/api/memories/{mid}")
    async def correct_memory(mid: str, body: Input):
        return {"id": memory.correct(mid, body.content)}

    @app.delete("/api/memories/{mid}")
    async def delete_memory(mid: str):
        memory.delete(mid)
        return {"status": "deleted"}

    @app.get("/api/memories/{mid}/evidence")
    async def evidence(mid: str):
        return memory.evidence(mid)

    frontend = Path(
        os.environ.get(
            "AGENT_FRONTEND_DIR", str(Path(__file__).resolve().parents[3] / "desktop" / "dist")
        )
    )
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="desktop")
    return app
