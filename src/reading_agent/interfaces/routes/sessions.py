"""sessions HTTP endpoints."""

from fastapi import Query, Request

from reading_agent.storage.database import Conflict

from .common import Input, run_event_response


def register(app, store, sessions, discussion):
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
