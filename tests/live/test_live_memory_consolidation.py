"""真实模型后台读取/合并记忆，正式用户数据不参与。"""

import json
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model
from reading_agent.services.memory import Memory
from reading_agent.services.memory_consolidation import ConsolidationPolicy
from reading_agent.storage.database import Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_real_background_memory_consolidation(tmp_path):
    store = Store(tmp_path / "reading.db")
    tid = store.topic("人工测试主题")
    async with httpx.AsyncClient(trust_env=False) as client:
        provider = ChatProvider(load_model(), client)
        memory = Memory(
            store,
            provider,
            consolidation_provider=provider,
            consolidation_policy=ConsolidationPolicy(
                min_new=3, cooldown_seconds=0, max_seconds=180
            ),
        )
        for content in ("用户熟悉 Java 编程。", "用户具有 Java 开发背景。", "用户熟悉Java。"):
            memory.add(content, topic_id=tid)
        assert memory.maybe_consolidate()
        await memory.consolidator.task
        status = memory.consolidator.status()
        report = json.loads(
            (
                memory.directory / ".consolidation" / "runs" / status["run_id"] / "report.json"
            ).read_text(encoding="utf-8")
        )
        (PROJECT_ROOT / "artifacts" / "memory-consolidation-live-result.json").write_text(
            json.dumps(
                {"state": status, "report": report, "memories": memory.list(tid)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        assert status["status"] == "completed", status["error"]
        assert status["processed_count"] == 3 and status["pending_count"] == 0
        assert any(
            t["name"] == "memory_file_read" and t["status"] == "success" for t in report["tools"]
        )
        assert any(
            t["name"] in {"memory_file_patch", "memory_file_write", "memory_file_create"}
            and t["status"] == "success"
            for t in report["tools"]
        ), "模型没有实际执行文件修改"
        assert not memory.maybe_consolidate(force=True)
        await memory.shutdown()
