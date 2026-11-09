"""仅绑定本机的应用接口；桌面随机令牌授权，后台任务不归连接所有。"""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

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

from .routes.common import Input
from .routes.materials import register as register_materials
from .routes.memory import register as register_memory
from .routes.sessions import register as register_sessions


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

    register_materials(app, store, service, jobs, memory)

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

    register_sessions(app, store, sessions, discussion)

    register_memory(app, memory)

    frontend = Path(
        os.environ.get(
            "AGENT_FRONTEND_DIR", str(Path(__file__).resolve().parents[3] / "desktop" / "dist")
        )
    )
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="desktop")
    return app
