"""讨论与精读共享的有界运行快照；草稿只供预览，成功结果由业务层提交。"""

import asyncio
import copy
import time
from dataclasses import dataclass, field

from reading_agent.agents.harness import RunReport
from reading_agent.storage.database import identity


@dataclass
class Running:
    id: str = field(default_factory=identity)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    draft: str = ""
    answer_draft: str = ""
    activities: list = field(default_factory=list)
    status_message: str = ""
    sequence: int = 0
    phase: str = "running"
    error: str | None = None
    task: asyncio.Task | None = None
    subscribers: set = field(default_factory=set)
    tool_pairs: list = field(default_factory=list)
    report: RunReport | None = field(default=None, repr=False)
    provider: object | None = field(default=None, repr=False)
    metrics: dict = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic, repr=False)

    def emit(self, kind, **data):
        if kind == "status" and data.get("message"):
            self.status_message = data["message"][:256]
        self.sequence += 1
        event = {"type": kind, "run_id": self.id, "sequence": self.sequence, **data}
        for queue in list(self.subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "snapshot", "data": self.snapshot()})
            else:
                queue.put_nowait(event)

    def snapshot(self):
        return {
            "run_id": self.id,
            "sequence": self.sequence,
            "draft": self.draft,
            "answer_draft": self.answer_draft,
            "activities": copy.deepcopy(self.activities),
            "status_message": self.status_message,
            "phase": self.phase,
            "error": self.error,
            "metrics": {
                **copy.deepcopy(self.metrics),
                "phase": self.phase,
                "elapsed_seconds": round(time.monotonic() - self.started, 3),
            }
            if self.phase == "running"
            else copy.deepcopy(self.metrics),
        }
