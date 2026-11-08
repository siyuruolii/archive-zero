"""仅桌面自动化使用的人工材料。不得在用户正式数据目录运行。"""

import asyncio
import json
import sys
from types import SimpleNamespace

from reading_agent.agents.models import RouteFinal, RouteItem
from reading_agent.agents.provider import ProviderEvent, ToolCall, Turn
from reading_agent.infrastructure.fetching import FeedItem
from reading_agent.services.discussion import Discussion
from reading_agent.services.materials import MaterialService
from reading_agent.services.sessions import Sessions
from reading_agent.storage.database import Store, identity, now


def seed(directory, *, observability=False):
    store = Store(directory + "/reading.db")
    tid = store.topic("Agent 工程", "读懂工具循环，检验权限、重试和取消的边界。")
    tag = store.tag(tid, "工具与运行")
    sid = identity()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO sources(id,source_key,name,entry_url,created_at) VALUES(?,?,?,?,?)",
            (sid, "rss:fixture", "桌面人工资料", "https://fixture.test/feed", now()),
        )
    service = MaterialService(store, None, None)
    mid = service.ingest(
        sid,
        [
            FeedItem(
                "fixture",
                "工具循环：让模型提出请求，让程序守住边界",
                "https://fixture.test/article",
                "人工桌面验证材料，不代表外部新闻。",
                None,
            )
        ],
    )[0]
    store.link(tid, sid)
    service.commit_route(
        mid,
        RouteFinal(
            status="succeeded",
            reason="人工桌面验证",
            routes=[
                RouteItem(
                    topic_id=tid,
                    tag_ids=[tag],
                    short_summary=(
                        "工具循环由模型请求、程序校验与执行、结果回传组成。"
                        "程序控制合法工具、Topic 作用域和总预算。资料中的指令不是授权；"
                        "临时网络失败可以有限重试，但不重放已完成的工具。"
                    ),
                    match_reason="人工验证",
                    is_recommended=True,
                    recommendation_reason="人工验证界面显示，不代表模型判断。",
                    needs_deep_read=False,
                    deep_read_guideline="",
                )
            ],
        ),
    )
    if observability:
        asyncio.run(seed_observability(store, service, mid, tid))


async def seed_observability(store, service, mid, tid):
    """走真实讨论/JSONL路径，供应商由确定性替身提供，绝不请求外部模型。"""

    class FixtureProvider:
        model_name = "桌面验证模型"
        settings = SimpleNamespace(
            context_window_tokens=1000000, context_byte_budget=131072, output_token_budget=4096
        )
        count = 0

        async def stream(self, request):
            self.count += 1
            if self.count == 1:
                turn = Turn(
                    calls=[
                        ToolCall(
                            "fixture-read",
                            "material_read",
                            json.dumps({"material_id": mid, "start": 0, "length": 100}),
                        )
                    ],
                    finish_reason="tool_calls",
                    usage={
                        "prompt_tokens": 1000,
                        "completion_tokens": 50,
                        "total_tokens": 1050,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                    },
                )
            else:
                turn = Turn(
                    content=json.dumps(
                        {
                            "status": "succeeded",
                            "reason": "人工验证",
                            "answer": "这是一条界面验证讨论：模型提出请求，"
                            "程序校验并执行工具，返回实际读取结果。"
                            "统计来自隔离测试替身，不代表真实模型用量。",
                        },
                        ensure_ascii=False,
                    ),
                    usage={
                        "prompt_tokens": 1200,
                        "completion_tokens": 200,
                        "total_tokens": 1400,
                        "prompt_cache_hit_tokens": 960,
                        "prompt_cache_miss_tokens": 240,
                    },
                )
            yield ProviderEvent("turn", turn=turn)

    service.provider = FixtureProvider()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO material_bodies VALUES(?,?,?)",
            (mid, "人工资料：模型发起工具请求，程序校验与执行，返回原文及范围。", now()),
        )
    sessions = Sessions(store)
    tm = store.one("SELECT id FROM topic_materials WHERE material_id=? AND topic_id=?", (mid, tid))[
        "id"
    ]
    created = sessions.create(tm)
    discussion = Discussion(store, sessions, service)
    bid = created["branch_id"]
    discussion.start(bid, "请读取正文并解释工具循环。", expected_leaf=sessions.path(bid)[-1]["id"])
    await discussion.runs[bid].task
    assert discussion.runs[bid].phase == "completed"


if __name__ == "__main__":
    seed(sys.argv[1], observability=True)
