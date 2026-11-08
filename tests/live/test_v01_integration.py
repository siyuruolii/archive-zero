"""固定 RSS 经真实模型、SQLite、讨论与记忆的闭环；显式 --run-live 才执行。"""

import json
from pathlib import Path

import httpx
import pytest

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import Task
from reading_agent.agents.provider import ChatProvider
from reading_agent.infrastructure.configuration import load_model
from reading_agent.infrastructure.fetching import Fetcher
from reading_agent.services.discussion import Discussion
from reading_agent.services.materials import MaterialService
from reading_agent.services.memory import Memory
from reading_agent.services.sessions import Sessions
from reading_agent.storage.database import Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.live
async def test_rss_database_discussion_memory_loop(tmp_path):
    article = (
        "工具循环包含模型请求、程序执行、结果回传。模型给出工具名和参数，"
        "程序校验角色、参数和作用域后执行。临时网络失败可有限重试，"
        "已完成工具不重复执行。预算耗尽时停止；取消必须阻止后续工具派发。"
        "这是人工测试材料，没有实验数据。"
    )
    feed = (
        "<rss><channel><item><guid>test-one</guid><title>工具循环与可靠性</title>"
        "<link>https://fixture.test/article</link><description>模型请求、程序校验与有限重试。</description>"
        "</item></channel></rss>"
    ).encode()

    def handle(request):
        return (
            httpx.Response(200, content=feed)
            if request.url.path == "/feed"
            else httpx.Response(200, text="<article>" + article + "</article>")
        )

    store = Store(tmp_path / "app.db")
    learn = store.topic("Agent 学习", "理解模型请求、程序执行与工具返回的核心机制。")
    engineering = store.topic("工程可靠性", "关注权限、预算、重试与取消。")
    store.tag(learn, "工具循环")
    store.tag(engineering, "运行边界")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as source_client,
        httpx.AsyncClient(trust_env=False) as model_client,
    ):
        provider = ChatProvider(load_model(), model_client)
        service = MaterialService(store, Fetcher(source_client), provider)
        memory = Memory(store, provider)
        service.memory = memory
        source = await service.add_source(
            "人工 RSS", "https://fixture.test/feed", [learn, engineering]
        )
        result = await service.fetch(source["id"])
        assert len(result["saved"]) == 1
        mid = result["saved"][0]
        assert (
            store.one("SELECT * FROM materials WHERE id=?", (mid,))["routing_status"] == "succeeded"
        )
        assert store.inbox(learn) and store.inbox(engineering)
        assert (await service.fetch(source["id"]))["saved"] == []
        sessions = Sessions(store)
        created = sessions.create(store.inbox(learn)[0]["id"])
        discussion = Discussion(store, sessions, service, memory)
        bid = created["branch_id"]
        question = "我熟悉Java，正在学习Python。请记住我的学习背景，并解释工具循环。"
        discussion.start(bid, question, expected_leaf=sessions.path(bid)[-1]["id"])
        await discussion.runs[bid].task
        assert discussion.runs[bid].phase == "completed", (
            discussion.runs[bid].error,
            discussion.runs[bid].report.validation_errors,
        )
        assert memory.last_extraction_error is None, memory.last_extraction_error
        rows = memory.list(learn, category="profile")
        assert rows and any("Java" in r["content"] for r in rows)
        assert not memory.list(engineering, category="profile"), "Topic 私有背景不允许跨 Topic 召回"
        compare_task = Task(
            role="discuss",
            material_id=mid,
            topic_ids=[learn],
            question="请解释工具循环，给出适合我的学习类比。",
        )
        baseline = await Harness(provider, service.corpus(mid, [learn])).run(compare_task)
        with_memory = await Harness(provider, service.corpus(mid, [learn]), memory=memory).run(
            compare_task
        )
        assert baseline.status == "succeeded", baseline.end_reason
        assert with_memory.status == "succeeded", with_memory.end_reason
        # 用户同时给出熟悉 Java 和学习 Python 两项背景；两种语言的适用类比都合法。
        # 不要求逐字包含 Java，避免把合理的 Python/DTO 解释误判为召回失败。
        assert any(
            language in with_memory.result["answer"].casefold() for language in ("java", "python")
        ), "回答未体现已召回的语言背景"
        destination = PROJECT_ROOT / "artifacts" / "v01-live-integration.json"
        destination.write_text(
            json.dumps(
                {
                    "model": provider.model_name,
                    "topic_count": 2,
                    "rss_saved": len(result["saved"]),
                    "memories": rows,
                    "without_memory": baseline.result,
                    "with_memory": with_memory.result,
                    "discussion_entries": len(sessions.path(bid)),
                    "extraction_error": memory.last_extraction_error,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
