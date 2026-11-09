"""长期记忆的索引检索与按 ID 展开；不开放任意文件读取。"""

from typing import Literal

from pydantic import Field

from reading_agent.domain.models import Identity, StrictModel

from .base import ToolSpec, error


class MemorySearchArgs(StrictModel):
    topic_id: Identity
    query: str = Field(min_length=1, max_length=200)


class MemoryReadArgs(StrictModel):
    memory_id: Identity


def memory_search(session, args):
    if args.topic_id not in session.task.topic_ids:
        return error("topic_not_in_scope")
    if not args.query.strip():
        return error("invalid_query")
    data = session.corpus.memory_loader(args.topic_id, args.query)
    for row in data:
        session.memory_grants[row["memory_id"]] = args.topic_id
    return {"status": "success", "scope": args.topic_id, "data": data}


def memory_read(session, args):
    if args.memory_id not in session.memory_grants:
        return error("memory_not_in_scope")
    topic_id = session.memory_grants[args.memory_id]
    data = session.corpus.memory_reader(args.memory_id, topic_id=topic_id)
    if data is None:
        return error("memory_not_found_in_scope")
    return {"status": "success", "scope": topic_id, "data": data}


TOOLS = (
    ToolSpec(
        "memory_search",
        "检索当前Topic及全局长期记忆，返回记忆ID、类别和简短预览。"
        "需要详情时用memory_read；不是记忆写入工具。",
        MemorySearchArgs,
        ("route", "discuss"),
        side_effect=False,
        guideline="需要了解用户明确背景、读过的材料或Agent既有阅读笔记时使用。只检索当前Topic及全局记录；需要正文时再用memory_read。",
        run=memory_search,
        available=lambda s: s.corpus.memory_loader is not None,
        cacheable=False,
    ),
    ToolSpec(
        "memory_read",
        "按本轮memory_search返回的memory_id展开长期记忆元数据和正文。"
        "不接受文件路径，也不构成本轮正文阅读证据。",
        MemoryReadArgs,
        ("route", "discuss"),
        side_effect=False,
        guideline="需核对记忆依据、版本、时间或正文时使用；只能读取本轮memory_search返回的ID。旧记忆不代表信息仍有效，也不构成材料原文阅读证据。",
        run=memory_read,
        available=lambda s: s.corpus.memory_reader is not None,
        cacheable=False,
    ),
)


class MemoryFileReadArgs(StrictModel):
    paths: list[Identity] = Field(min_length=1, max_length=5)


class MemoryFileWriteArgs(StrictModel):
    path: Identity
    text: str = Field(max_length=256000)


class MemoryFilePatchArgs(StrictModel):
    path: Identity
    old_text: str = Field(min_length=1, max_length=16000)
    new_text: str = Field(max_length=16000)


class MemoryFileCreateArgs(StrictModel):
    category: Literal["profile", "user_reading", "agent_reading"]
    topic_id: Identity | None
    source_ids: list[Identity] = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=8000)


def workspace_operation(session, operation, *args):
    try:
        return {
            "status": "success",
            "data": getattr(session.corpus.memory_workspace, operation)(*args),
        }
    except ValueError as exc:
        code = str(exc)
        if not code.startswith("workspace_") or not code.replace("_", "").isalnum():
            code = "workspace_invalid_document"
        return error(code)


TOOLS += (
    ToolSpec(
        "memory_file_read",
        "仅沉淀角色：读取工作副本清单内的MD文件，可批量最多5个。",
        MemoryFileReadArgs,
        ("consolidate",),
        side_effect=False,
        guideline="整理前先读取清单内全部pending记忆；修改任何已有文件之前也必须先读取。路径仅限工作副本清单。",
        run=lambda s, a: workspace_operation(s, "read", a.paths),
        available=lambda s: s.corpus.memory_workspace is not None,
        cacheable=False,
    ),
    ToolSpec(
        "memory_file_write",
        "仅沉淀角色：写回已读取的工作副本MD；保留front matter身份、依据和范围。",
        MemoryFileWriteArgs,
        ("consolidate",),
        guideline="需要整体整理已读取文件时使用；只写工作副本，保留身份、依据和范围。用户原始阅读动作与程序读取凭据不能改写。",
        run=lambda s, a: workspace_operation(s, "write", a.path, a.text),
        available=lambda s: s.corpus.memory_workspace is not None,
        cacheable=False,
    ),
    ToolSpec(
        "memory_file_patch",
        "仅沉淀角色：精确替换工作副本中唯一出现的文本；必须先读取文件。",
        MemoryFilePatchArgs,
        ("consolidate",),
        guideline="仅需局部修订已读取文件时优先使用；old_text必须唯一匹配。不能借补丁改变身份、来源或扩大Topic范围。",
        run=lambda s, a: workspace_operation(s, "patch", a.path, a.old_text, a.new_text),
        available=lambda s: s.corpus.memory_workspace is not None,
        cacheable=False,
    ),
    ToolSpec(
        "memory_file_create",
        "仅沉淀角色：依据已读取的source_ids创建同类同范围的整理条目，程序生成ID和元数据。",
        MemoryFileCreateArgs,
        ("consolidate",),
        guideline="需要从已读取来源生成整理条目时使用；source_ids必须可核验，新条目与来源保持同类同范围，不凭空增加用户事实。",
        run=lambda s, a: workspace_operation(
            s, "create", a.category, a.topic_id, a.source_ids, a.content
        ),
        available=lambda s: s.corpus.memory_workspace is not None,
        cacheable=False,
    ),
)
