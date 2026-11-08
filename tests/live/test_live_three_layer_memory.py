"""真实模型按工具召回历史会话、语义提炼、阅读及 MD 沉淀。"""

import json
from pathlib import Path

import httpx
import pytest
from test_sessions_memory import append

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model
from reading_agent.services.memory import Memory

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_real_three_layer_memory_recall_and_markdown(state):
    store, service, sessions, memory, old, tid, tm = state
    append(sessions, old["branch_id"], "之前讨论 Harness：保存成功后才能提炼记忆。")
    append(
        sessions,
        old["branch_id"],
        "原因是保存可能因分支末端变化而失败，不能沉淀未提交的结果。",
        "assistant",
    )
    current = sessions.create(tm)
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    question = (
        "必须调用 session_search，关键词用 Harness，limit=1、topic_id=null，回忆另一会话的决定；"
        "再调用session_read用返回的会话及分支ID从start=0读limit=2条原始消息；"
        "必须memory_search用当前Topic检索Java，并memory_read展开返回的记忆ID；"
        "并调用 material_read 从0开始读取当前材料。根据真实历史解释为什么保存后才能提炼记忆，"
        "保留来源会话ID。不要声称用户掌握了文章。"
    )
    leaf = append(sessions, current["branch_id"], question)
    memory.add("用户熟悉Java，正在学习Python", topic_id=tid)

    def commit(report):
        if report.status == "succeeded":
            sessions.append(
                current["branch_id"],
                leaf,
                [("message", {"role": "assistant", "content": report.result["answer"]})],
            )

    async with httpx.AsyncClient(trust_env=False) as client:
        provider = ChatProvider(load_model(), client)
        memory.provider = provider
        report = await Harness(
            provider,
            lambda task: service.corpus(task.material_id, task.topic_ids),
            Budget(max_model_calls=8, max_seconds=180),
            memory=memory,
            sessions=sessions,
        ).run(
            Task(role="discuss", material_id=mid, topic_ids=[tid], question=question),
            branch_id=current["branch_id"],
            question_entry_id=leaf,
            commit=commit,
        )
    destination = PROJECT_ROOT / "artifacts" / "memory-three-layer-live-result.json"
    destination.write_text(
        json.dumps(
            {
                "status": report.status,
                "reason": report.end_reason,
                "validation_errors": report.validation_errors,
                "attempts": report.attempts,
                "memory_status": report.memory_status,
                "memory_error": report.memory_error,
                "reading_memory_status": report.reading_memory_status,
                "reading_memory_error": report.reading_memory_error,
                "result": report.result,
                "recall": [t["result"] for t in report.tools if t["name"] == "session_search"],
                "markdown_files": [
                    str(p.relative_to(memory.directory)) for p in memory.directory.rglob("*.md")
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.status == "succeeded", (report.end_reason, report.validation_errors)
    assert {"memory_search", "memory_read", "session_search", "session_read", "material_read"} <= {
        t["name"] for t in report.tools
    }, [t["name"] for t in report.tools]
    assert report.memory_status == "completed", report.memory_error
    assert report.reading_memory_status == "completed", report.reading_memory_error
    recalls = [t["result"]["data"] for t in report.tools if t["name"] == "session_search"]
    assert recalls and any(r["session_id"] == old["session_id"] for data in recalls for r in data)
    assert all(r["session_id"] != current["session_id"] for data in recalls for r in data)
    assert any(
        r["recall_status"] == "summarized" and r["evidence"] for data in recalls for r in data
    )
    assert any(a["purpose"] == "session_recall" for a in report.attempts)
    assert memory.list(tid, category="agent_reading")
    assert Memory(store, None).list(tid, category="profile")
