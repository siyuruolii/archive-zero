"""渐进式 schema 加载，仅搜索本轮已经授权的工具。"""

from pydantic import Field

from reading_agent.domain.models import StrictModel

from .base import ToolSpec
from .filesystem import available


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(ge=1, le=5)


def search_tool(session, args):
    from .registry import REGISTRY

    words = args.query.casefold().split()
    found = []
    for name in session.eligible_names:
        if name == "search_tool":
            continue
        text = (name + " " + REGISTRY[name].api_description).casefold()
        score = sum(word in text for word in words)
        if score:
            found.append((score + (10 if name.casefold() == args.query.casefold() else 0), name))
    selected = [
        name for _, name in sorted(found, key=lambda pair: (-pair[0], pair[1]))[: args.limit]
    ]
    session.loaded_tools.update(selected)
    return {
        "status": "success",
        "error": None,
        "data": [
            {
                "name": name,
                "description": REGISTRY[name].description,
                "guideline": REGISTRY[name].guideline,
            }
            for name in selected
        ],
    }


TOOLS = (
    ToolSpec(
        "search_tool",
        "发现本轮有权限的工具并加载 schema；支持工具名或用途关键词。",
        SearchArgs,
        ("discuss",),
        side_effect=False,
        guideline=(
            "查找文件编辑、bash、材料检索、记忆或会话工具。"
            "零命中可换原样关键词；发现后下一轮可调用，bash仍逐次等待用户确认。"
        ),
        run=search_tool,
        available=available,
        cacheable=False,
    ),
)
