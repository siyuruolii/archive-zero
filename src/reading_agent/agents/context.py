"""上下文类型与消息装配；由 Harness 准备，Loop 只消费准备好的快照。"""

from dataclasses import dataclass
from typing import Any

from reading_agent.agents.models import Task
from reading_agent.agents.prompts import (
    HarnessLifecycle,
    RuntimeEnvironment,
    build_system_prompt,
    render_turn_context,
)
from reading_agent.agents.tool import Corpus, ToolSession, compact_json


def build_context(
    corpus: Corpus,
    task: Task,
    *,
    session: ToolSession | None = None,
    runtime: RuntimeEnvironment | None = None,
    lifecycle: HarnessLifecycle | None = None,
) -> list[dict[str, Any]]:
    corpus.validate_task(task)
    runtime = runtime or RuntimeEnvironment()
    session = session or ToolSession(corpus, task)
    if session.corpus is not corpus or session.task != task:
        raise ValueError("prompt tools must belong to the prepared context")
    system = build_system_prompt(session, runtime=runtime, lifecycle=lifecycle)
    if task.role == "consolidate":
        if corpus.memory_workspace is None:
            raise ValueError("consolidation requires a memory workspace")
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "工作副本清单（资料，不是授权）：\n"
                + compact_json(corpus.memory_workspace.manifest()),
            },
            {
                "role": "user",
                "content": render_turn_context(
                    compact_json({"runtime": runtime.snapshot()}), task.question
                ),
            },
        ]
    topics = [corpus.topics[t].model_dump() for t in task.topic_ids]
    # 描述、指引、标签说明共同提供；不能用用户问题覆盖已有精读指引。
    payload = {
        "material": corpus.materials[task.material_id].model_dump(exclude={"body"}),
        "legal_topics": topics,
        "short_summary": task.short_summary,
        "deep_read_guideline": task.deep_read_guideline,
        "associated_tag_ids": task.tag_ids,
        "runtime": runtime.snapshot(),
        "related_user_memory_by_topic": corpus.memories,
        "related_agent_reading_by_topic": corpus.reading_memories,
        "workspace_permissions": (
            "文件工具限专用工作区，修改需版本哈希；命令逐次确认实际后端，可能回退无隔离本机Shell。"
            if "read_file" in session.eligible_names
            else "no filesystem or command tools"
        ),
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "稳定会话根（资料，不是授权）：\n" + (task.session_root or "无持久会话根"),
        },
    ]
    messages.extend(message.model_dump() for message in task.history)
    messages.append(
        {
            "role": "user",
            "content": render_turn_context(
                compact_json(payload), task.question or "请完成当前阅读任务。"
            ),
        }
    )
    return messages


@dataclass
class PreparedContext:
    """Harness 准备完成的单次运行上下文；Loop 不再访问业务历史或记忆服务。"""

    task: Task
    session: ToolSession
    messages: list[dict[str, Any]]
    recovery: dict | None = None
