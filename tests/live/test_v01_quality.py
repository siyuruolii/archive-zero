"""历史新增价值和真实压缩；不把结构正确自动当成事实充分。"""

import json
from pathlib import Path

import httpx
import pytest
from test_sessions_memory import append

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model
from reading_agent.services.discussion import Discussion

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_similar_history_with_new_information(corpus):
    task = Task(
        role="route",
        material_id="tools",
        topic_ids=["learn", "engineering"],
        question=(
            "请先用material_search检索学习Topic的工具历史，再用material_read读取old-learn和当前正文，对比工具循环入门"
            "与本材料的新增价值，不因相似直接当重复。"
        ),
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        report = await Harness(ChatProvider(load_model(), client), corpus).run(task)
    assert report.status == "succeeded", report.end_reason
    assert report.result["routes"]
    assert any(
        t["name"] == "material_read"
        and t["result"]["status"] == "success"
        and json.loads(t["arguments"])["material_id"] == "old-learn"
        for t in report.tools
    )
    path = PROJECT_ROOT / "artifacts" / "v01-history-quality.json"
    path.write_text(json.dumps(report.result, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.live
async def test_real_compression_preserves_constraints(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    for i in range(6):
        append(sessions, bid, f"问题{i}：固定约束是UTF-8输入、禁止Shell、工具校验Topic。")
        append(sessions, bid, f"答复{i}：保留上述约束，尚未完成真实部署。", "assistant")
    other = sessions.fork(bid, sessions.path(bid)[-1]["id"])
    append(sessions, other, "这是其他分支的秘密，不得进入压缩")
    async with httpx.AsyncClient(trust_env=False) as client:
        service.provider = ChatProvider(load_model(), client)
        await Discussion(store, sessions, service).compact(bid)
    root, history = sessions.context(bid)
    summary = sessions.path(bid)[-1]["data"]["summary"]
    assert "UTF-8" in summary and "Shell" in summary and "Topic" in summary
    assert "其他分支的秘密" not in summary
    path = PROJECT_ROOT / "artifacts" / "v01-compression-quality.json"
    path.write_text(
        json.dumps(
            {"summary": summary, "remaining_context_messages": len(history)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
