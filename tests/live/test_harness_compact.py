"""真实模型压缩 + Loop 后续任务；超长异常恢复另由受控 HTTP 错误验证。"""

import json
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.compact import input_bytes
from reading_agent.agents.context import build_context
from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import FINAL_MODELS, HistoryMessage, Task
from reading_agent.agents.provider import ChatProvider, ModelRequest
from reading_agent.agents.tool import ToolSession
from reading_agent.infrastructure.configuration import load_model

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_real_loop_compaction_keeps_current_task_and_constraints(corpus):
    history = []
    for index in range(4):
        history.extend(
            [
                HistoryMessage(
                    role="user",
                    content=f"旧问题{index}，必须保留UTF-8、禁止Shell。" + "历史说明。" * 100,
                ),
                HistoryMessage(
                    role="assistant",
                    content=f"已记录问题{index}，尚未完成实际部署。" + "已讨论内容。" * 100,
                ),
            ]
        )
    task = Task(
        role="discuss",
        material_id="tools",
        topic_ids=["learn"],
        history=history,
        question="用两句话说明此前任务的约束与未完成项，不需要读取原文。",
        session_root="稳定根：学习工具循环。",
    )
    request = ModelRequest(
        build_context(corpus, task),
        ToolSession(corpus, task).schemas(),
        FINAL_MODELS[task.role].model_json_schema(),
        4096,
    )
    budget = Budget(context_token_budget=input_bytes(request) + 4096 + 100)
    async with httpx.AsyncClient(trust_env=False) as client:
        report = await Harness(ChatProvider(load_model(), client), corpus, budget).run(task)
    path = PROJECT_ROOT / "artifacts" / "harness-live-compact.json"
    path.write_text(
        json.dumps(
            {
                "status": report.status,
                "end_reason": report.end_reason,
                "model_calls": report.model_calls,
                "attempts": report.attempts,
                "compactions": report.compaction_events,
                "result": report.result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.status == "succeeded", report.end_reason
    assert any(e["changed"] for e in report.compaction_events)
    assert "UTF-8" in report.result["answer"] and "Shell" in report.result["answer"]
    assert any(a["purpose"] == "compaction" for a in report.attempts)
