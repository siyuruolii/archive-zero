"""memory HTTP endpoints."""

from fastapi import Query

from .common import Input


def register(app, memory):
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
