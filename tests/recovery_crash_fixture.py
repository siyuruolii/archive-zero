"""仅在隔离目录制造硬退出，验证 JSONL 恢复；不得用于正式数据。"""

import asyncio
import json
import os
import sys
from pathlib import Path

from conftest import ScriptedProvider
from desktop_fixture import seed
from test_runtime import tool_turn

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.models import Task
from reading_agent.services.materials import MaterialService
from reading_agent.services.memory import Memory
from reading_agent.services.sessions import Sessions
from reading_agent.storage.database import Store, identity


async def main(folder):
    folder = Path(folder).resolve()
    if folder == Path(__file__).resolve().parents[1] / ".local" or (folder / "reading.db").exists():
        raise ValueError("fresh isolated data directory required")
    seed(str(folder), observability=False)
    store = Store(folder / "reading.db")
    sessions = Sessions(store)
    memory = Memory(store, None)
    service = MaterialService(store, None, None)
    tm = store.one("SELECT * FROM topic_materials")
    created = sessions.create(tm["id"])
    bid = created["branch_id"]
    question = "验证保存笔记被意外中断后可以续跑。"
    leaf = sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": "user", "content": question})]
    )
    run_id = identity()
    sessions.record_event(bid, run_id, "run_started", {"question_entry_id": leaf})
    provider = ScriptedProvider(
        tool_turn("search_tool", {"query": "write_file", "limit": 1}, "discover"),
        tool_turn(
            "write_file", {"path": "note.txt", "content": "hello", "expected_sha256": None}, "write"
        ),
    )
    (folder / "recovery-info.json").write_text(
        json.dumps(
            {
                "branch_id": bid,
                "run_id": run_id,
                "material_id": tm["material_id"],
                "topic_id": tm["topic_id"],
            }
        ),
        encoding="utf-8",
    )

    def observe(event):
        sessions.record_event(bid, run_id, event.kind, event.data)
        if event.kind == "tool_result" and event.data["name"] == "write_file":
            os._exit(73)  # 结果已fsync，后续检查点与最终回答尚未提交。

    await Harness(
        provider,
        lambda task: service.corpus(task.material_id, task.topic_ids, persist=True),
        Budget(context_token_budget=60000, max_model_calls=10, max_seconds=60),
        sessions=sessions,
        memory=memory,
    ).run(
        Task(
            role="discuss",
            material_id=tm["material_id"],
            topic_ids=[tm["topic_id"]],
            question=question,
        ),
        branch_id=bid,
        question_entry_id=leaf,
        on_event=observe,
        commit=lambda report: None,
    )
    raise AssertionError("hard exit was not reached")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
