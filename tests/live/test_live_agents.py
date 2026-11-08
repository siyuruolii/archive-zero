"""真实服务验收入口，默认跳过。结构检查通过不代替人工阅读质量判断。"""

import json
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
@pytest.mark.parametrize(
    ("role", "material", "expected_status"),
    [
        ("route", "tools", "succeeded"),
        ("route", "short", "succeeded"),
        ("route", "injection", "succeeded"),
        ("deep_read", "tools", "succeeded"),
        ("deep_read", "missing", "failed"),
        ("discuss", "tools", "succeeded"),
    ],
)
async def test_live_role_contract(corpus, role, material, expected_status):
    # --run-live 后缺配置应失败，不能悄悄跳过并冒充验证完成。
    settings = load_model()
    task = Task(
        role=role,
        material_id=material,
        topic_ids=["learn", "engineering"] if role == "route" else ["learn"],
        question="解释工具循环的职责边界。请需要时实际读取资料。" if role == "discuss" else "",
        deep_read_guideline="解释程序如何执行工具、检查权限及控制重试。"
        if role == "deep_read"
        else "",
    )
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        report = await Harness(ChatProvider(settings, client), corpus, Budget()).run(task)
    # 固定样例只保留最终产物和安全观测元数据，不写入密钥、提示词或工具正文。
    destination = PROJECT_ROOT / "artifacts" / "stage1-live"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f"{role}-{material}.json").write_text(
        json.dumps(
            {
                "role": role,
                "material_id": material,
                "model": report.model,
                "topic_ids": task.topic_ids,
                "status": report.status,
                "end_reason": report.end_reason,
                "result": report.result,
                "model_calls": report.model_calls,
                "tool_calls": report.tool_calls,
                "elapsed_seconds": report.elapsed_seconds,
                "usage": report.usage,
                "usage_complete": report.usage_complete,
                "readings": report.readings,
                "validation_errors": report.validation_errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # 失败信息只打印安全元数据，不默认长期保存提示词或原文。
    assert report.status == expected_status, (report.end_reason, report.validation_errors)
    assert report.result is not None, report.end_reason
    if role == "deep_read" and expected_status == "succeeded":
        assert report.readings
    if role == "route":
        assert report.result["routes"], "这些样例与至少一个合法 Topic 有关，零命中需人工调查"
    if expected_status == "failed":
        assert report.end_reason == "model_reported_failure"
