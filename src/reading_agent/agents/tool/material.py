"""材料搜索与正文读取，共用一个业务模块。"""

import hashlib

import httpx
from pydantic import Field

from reading_agent.domain.models import Identity, StrictModel

from ..models import Coverage
from .base import ToolSpec, error


class MaterialReadArgs(StrictModel):
    material_id: Identity
    start: int = Field(ge=0)
    length: int = Field(ge=1, le=4000)


class MaterialSearchArgs(StrictModel):
    topic_id: Identity
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(ge=1, le=5)


def material_search(session, args):
    if args.topic_id not in session.task.topic_ids:
        return error("topic_not_in_scope")
    query = args.query.strip().casefold()
    if not query:
        return error("invalid_query")
    matches = sorted(
        (
            h
            for h in session.corpus.history.values()
            if h.topic_id == args.topic_id and query in (h.title + " " + h.short_summary).casefold()
        ),
        key=lambda h: (h.published_at, h.id),
        reverse=True,
    )
    selected = matches[: args.limit]
    # 只有真正返回的结果获得正文读取资格，不能猜其他历史 ID。
    session.readable_ids.update(h.material_id for h in selected)
    return {
        "status": "success",
        "scope": args.topic_id,
        "truncated": len(matches) > args.limit,
        "data": [
            {
                "id": h.id,
                "material_id": h.material_id,
                "topic_id": h.topic_id,
                "title": h.title,
                "preview": h.short_summary[:240],
                "published_at": h.published_at,
            }
            for h in selected
        ],
    }


def material_read(session, args):
    if args.material_id not in session.readable_ids:
        return error("material_not_in_scope")
    material = session.corpus.materials.get(args.material_id)
    if material is None or not material.body or not material.body.strip():
        return error("body_unavailable")
    body = material.body
    if args.start >= len(body):
        return error("range_out_of_bounds")
    end = min(args.start + args.length, len(body))
    if not body[args.start : end].strip():
        return error("empty_reading_range")
    session.reads.append(Coverage(material_id=material.id, start=args.start, end=end))
    return {
        "status": "success",
        "data": {
            "material_id": material.id,
            "source_url": material.url,
            "text": body[args.start : end],
            "start": args.start,
            "end": end,
            "total_chars": len(body),
            "version_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "truncated": args.start > 0 or end < len(body),
            "scope": session.task.topic_ids,
        },
    }


async def material_read_async(session, args):
    if args.material_id not in session.readable_ids:
        return error("material_not_in_scope")
    material = session.corpus.materials.get(args.material_id)
    if material is not None and not material.body and session.corpus.body_loader:
        try:
            body = await session.corpus.body_loader(args.material_id)
        except httpx.TransportError:
            return error("transient_network_error")
        except httpx.HTTPStatusError as exc:
            return error(
                "transient_network_error"
                if exc.response.status_code == 429 or exc.response.status_code >= 500
                else "body_unavailable"
            )
        if body:
            material.body = body
    return material_read(session, args)


TOOLS = (
    ToolSpec(
        "material_read",
        "按授权材料ID分段读取正文；start/length是Unicode字符位置。",
        MaterialReadArgs,
        ("route", "deep_read", "discuss"),
        side_effect=False,
        retry_errors=("transient_network_error", "tool_timeout", "workspace_busy"),
        guideline="核实原文、引用证据或精读时使用；当前材料可直接读取，历史材料须先经material_search取得资格。标题/摘要足够时避免无必要读取。",
        run=material_read,
        arun=material_read_async,
    ),
    ToolSpec(
        "material_search",
        "在合法Topic按原样短语搜索材料标题和摘要，返回简短预览。"
        "仅返回的material_id可继续用material_read读取正文；零命中不代表没有相似材料。",
        MaterialSearchArgs,
        ("route", "discuss"),
        side_effect=False,
        guideline="需要对比历史材料或查重时使用。只搜索材料标题/摘要，不查会话；零命中只表示本次短语未找到，不等于没有相似内容。",
        run=material_search,
    ),
)
