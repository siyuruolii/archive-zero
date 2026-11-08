"""会话回忆与按真实分支展开原始消息，共用一个业务模块。"""

from pydantic import Field

from ..models import Identity, StrictModel
from .base import ToolSpec, error


class SessionSearchArgs(StrictModel):
    query: str = Field(max_length=200)
    topic_id: Identity | None
    limit: int = Field(ge=1, le=5)


class SessionReadArgs(StrictModel):
    session_id: Identity
    branch_id: Identity
    start: int = Field(ge=0)
    limit: int = Field(ge=1, le=10)


async def session_search(session, args):
    results = await session.corpus.session_loader(
        args.query, topic_id=args.topic_id, limit=args.limit, summarize=session.summarize
    )
    data = []
    for row in results:
        session.session_grants[(row["session_id"], row["branch_id"])] = row
        summary = row.get("summary") or ""
        evidence = row.get("evidence", [])
        data.append(
            {
                key: row[key]
                for key in (
                    "session_id",
                    "branch_id",
                    "leaf_entry_id",
                    "topic_id",
                    "title",
                    "recall_status",
                    "summary_error",
                )
            }
        )
        data[-1].update(
            summary=summary[:2000] or None,
            snippet=row["snippet"][:600],
            evidence=[{"entry_id": e["entry_id"], "quote": e["quote"][:300]} for e in evidence[:3]],
            truncated=(
                len(summary) > 2000
                or len(row["snippet"]) > 600
                or len(evidence) > 3
                or any(len(e["quote"]) > 300 for e in evidence[:3])
            ),
        )
    return {"status": "success", "scope": "user_sessions", "topic_id": args.topic_id, "data": data}


def cache_valid(session, response):
    sources = [
        session.session_grants.get((r["session_id"], r["branch_id"])) for r in response["data"]
    ]
    return (
        bool(sources)
        and all(sources)
        and (session.corpus.session_validator is None or session.corpus.session_validator(sources))
    )


def session_read(session, args):
    grant = session.session_grants.get((args.session_id, args.branch_id))
    if grant is None:
        return error("session_not_in_scope")
    if session.corpus.session_validator and not session.corpus.session_validator([grant]):
        return error("session_changed_or_deleted")
    data = session.corpus.session_reader(
        args.session_id,
        args.branch_id,
        expected_leaf=grant["leaf_entry_id"],
        start=args.start,
        limit=args.limit,
    )
    if data is None:
        return error("session_changed_or_deleted")
    if data.get("range_error"):
        return error("range_out_of_bounds")
    return {"status": "success", "scope": "user_sessions", "data": data}


TOOLS = (
    ToolSpec(
        "session_search",
        "关键词回忆本用户其他会话，返回有来源的语义归纳和简短预览。"
        "topic_id=null搜索所有主题；需要原始消息时用session_read；零命中不代表从未讨论。",
        SessionSearchArgs,
        ("discuss",),
        side_effect=False,
        guideline="问题涉及以前讨论、跨材料联系或历史决定时使用；先关键词召回，再基于带来源的片段归纳。topic_id=null可跨主题，零命中不证明从未讨论。",
        arun=session_search,
        available=lambda s: s.corpus.session_loader is not None,
        validate_cache=cache_valid,
    ),
    ToolSpec(
        "session_read",
        "按本轮session_search返回的session_id和branch_id展开原始消息。"
        "start为该分支普通消息的零基序号，limit最多10；不会合并其他分支。",
        SessionReadArgs,
        ("discuss",),
        side_effect=False,
        guideline="搜索预览不足、需要原话或核对历史决定时使用；仅展开本轮session_search授予的会话/分支，不能把其他分支决定当作当前决定。",
        run=session_read,
        available=lambda s: s.corpus.session_reader is not None,
        cacheable=False,
    ),
)
