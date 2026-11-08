"""Harness 注入的资料快照与只读加载接口。"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from ..models import HistoricalMaterial, Material, Task, Topic


@dataclass
class Corpus:
    topics: Mapping[str, Topic]
    materials: Mapping[str, Material]
    history: Mapping[str, HistoricalMaterial] = field(default_factory=dict)
    body_loader: Callable[[str], Awaitable[str | None]] | None = None
    memories: dict = field(default_factory=dict)
    memory_loader: Callable | None = None
    reading_memories: dict = field(default_factory=dict)
    session_loader: Callable | None = None
    session_validator: Callable | None = None
    memory_reader: Callable | None = None
    session_reader: Callable | None = None
    memory_workspace: object | None = None
    workspace: object | None = None
    command_approval: Callable | None = None
    permission_policy: object | None = None

    def validate_task(self, task: Task) -> None:
        if task.material_id not in self.materials:
            raise ValueError("unknown current material")
        if any(t not in self.topics for t in task.topic_ids):
            raise ValueError("unknown Topic")
        legal_tags = {tag.id for t in task.topic_ids for tag in self.topics[t].tags}
        if not set(task.tag_ids) <= legal_tags:
            raise ValueError("task contains tags outside Topic")
