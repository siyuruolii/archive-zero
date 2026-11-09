import asyncio
import json

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


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
