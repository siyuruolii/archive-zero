"""使用本机设置的模型验证阶段解析和可观测缓存，不要求缓存必须命中。"""

import json
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.harness import Harness
from reading_agent.agents.model_router import ModelRouter
from reading_agent.agents.models import Task
from reading_agent.agents.prompts import RuntimeEnvironment
from reading_agent.infrastructure.configuration import load_catalog


@pytest.mark.live
async def test_configured_model_reuses_stable_input_and_reports_cache(corpus):
    reports = []
    async with httpx.AsyncClient(trust_env=False) as client:
        router = ModelRouter(load_catalog(), client)
        harness = Harness(router, corpus, runtime=RuntimeEnvironment())
        task = Task(role="route", material_id="short", topic_ids=["learn", "engineering"])
        for _ in range(3):
            report = await harness.run(task)
            reports.append(
                {
                    "status": report.status,
                    "end_reason": report.end_reason,
                    "model": report.model,
                    "attempts": report.attempts,
                    "validation_errors": report.validation_errors,
                }
            )
            if report.status != "succeeded":
                break
        stats = {name: dict(provider.statistics) for name, provider in router.providers.items()}
    path = Path(__file__).resolve().parents[2] / "artifacts" / "model-profiles-live-result.json"
    path.write_text(
        json.dumps({"reports": reports, "statistics": stats}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert len(reports) == 3 and all(r["status"] == "succeeded" for r in reports), reports
    assert all(a["cache_status"] in {"reported", "unknown"} for r in reports for a in r["attempts"])
