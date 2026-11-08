"""真实模型讨论的事件流写 JSONL，SQL 仅保存定位/状态与检索投影。"""

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model
from reading_agent.services.discussion import Discussion

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_real_discussion_records_jsonl_and_sql_metadata(state):
    store, service, sessions, memory, created, _, _ = state
    bid, sid = created["branch_id"], created["session_id"]
    async with httpx.AsyncClient(trust_env=False) as client:
        service.provider = ChatProvider(load_model(), client)
        memory.provider = service.provider
        discussion = Discussion(store, sessions, service, memory)
        discussion.start(
            bid,
            "请必须调用material_read读取正文，再用两句话解释工具循环。",
            expected_leaf=sessions.path(bid)[-1]["id"],
        )
        run = discussion.runs[bid]
        await run.task
    events = []
    after = 0
    while True:
        page = sessions.events(sid, run_id=run.id, after=after, limit=200)
        if not page:
            break
        events.extend(page)
        after = page[-1]["sequence"]
        assert len(events) <= 3000
    counts = Counter(e["type"] for e in events)
    result = {
        "phase": run.phase,
        "error": run.error,
        "event_counts": dict(counts),
        "run_metadata": store.one("SELECT * FROM session_runs WHERE id=?", (run.id,)),
        "end_reason": run.report.end_reason if run.report else None,
        "validation_errors": run.report.validation_errors if run.report else [],
        "jsonl_bytes": sessions.journal.path(sid).stat().st_size,
        "observability": run.metrics,
    }
    (PROJECT_ROOT / "artifacts" / "session-jsonl-live-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert run.phase == "completed", result
    assert events[0]["type"] == "agent.run_started" and events[-1]["type"] == "agent.run_finished"
    assert counts["agent.text_delta"] and counts["agent.model_turn_received"]
    assert counts["agent.answer_delta"]
    streamed_answer = "".join(
        e["data"]["text"] for e in events if e["type"] == "agent.answer_delta"
    )
    assert streamed_answer.strip() == sessions.path(bid)[-1]["data"]["content"]
    assert counts["agent.tool_requested"] and counts["agent.tool_result"]
    assert counts["agent.telemetry"]
    assert run.metrics["model_calls"] == run.report.model_calls
    assert run.metrics["tool_calls"] == run.report.tool_calls
    assert run.metrics["context"]["input_bytes"] > 0
    assert sessions.last_run(bid)["metrics"]["cache"] == run.metrics["cache"]
    assert result["run_metadata"]["status"] == "completed"
    assert all(
        "content" not in json.loads(r["data"])
        for r in store.all("SELECT data FROM entries WHERE session_id=?", (sid,))
    )
    assert sessions.path(bid)[-1]["data"]["role"] == "assistant"
    await memory.shutdown()
