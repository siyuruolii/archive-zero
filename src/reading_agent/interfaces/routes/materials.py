"""materials HTTP endpoints."""

from fastapi import HTTPException, Request

from .common import Input, run_event_response


def register(app, store, service, jobs, memory):
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
