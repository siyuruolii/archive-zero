import json

import pytest
from conftest import ScriptedProvider, route_item, route_json
from pydantic import ValidationError
from test_sessions_memory import append

from reading_agent.agents.context import build_context
from reading_agent.agents.harness import Harness
from reading_agent.agents.models import DeepFinal, RouteFinal, Task
from reading_agent.agents.provider import ProviderError, ToolCall, Turn
from reading_agent.agents.tool import ToolSession
from reading_agent.agents.tool.registry import REGISTRY
from reading_agent.domain.models import HistoricalMaterial


@pytest.mark.parametrize(
    "routes",
    [
        [route_item("private")],
        [route_item(tag_ids=["eng-runtime"])],
        [route_item(), route_item()],
    ],
)
def test_route_rejects_outside_topic_tag_or_duplicate(corpus, routes):
    session = ToolSession(corpus, Task(role="route", material_id="tools", topic_ids=["learn"]))
    with pytest.raises(ValueError):
        session.validate_final(RouteFinal.model_validate_json(route_json(routes)))


def test_failure_cannot_include_partial_routing():
    with pytest.raises(ValidationError):
        RouteFinal.model_validate_json(route_json([route_item()], status="failed"))


@pytest.mark.parametrize(
    "changes",
    [
        {"is_recommended": "false"},
        {"deep_read_guideline": " "},
        {"short_summary": " "},
        {"tag_ids": ["x", "x"]},
        {"unknown": True},
    ],
)
def test_structured_route_types_are_strict(changes):
    item = route_item() | changes
    with pytest.raises(ValidationError):
        RouteFinal.model_validate_json(route_json([item]))


def test_body_range_and_cache(corpus):
    session = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    args = '{"material_id":"tools","start":0,"length":20}'
    result = session.execute("material_read", args)
    assert result["data"]["text"] == corpus.materials["tools"].body[:20]
    assert result["data"]["truncated"] is True
    assert result["data"]["end"] == 20
    assert session.execute("material_read", args) == result
    assert len(session.reads) == 1


@pytest.mark.parametrize(
    "args",
    [
        '{"material_id":"tools","start":-1,"length":20}',
        '{"material_id":"tools","start":0,"length":4001}',
        '{"material_id":"tools","start":"0","length":20}',
        '{"material_id":"tools","start":0,"length":20,"url":"file://secret"}',
        "{bad json",
    ],
)
def test_invalid_tool_arguments_do_not_read(corpus, args):
    session = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    assert session.execute("material_read", args)["error"] == "invalid_arguments"
    assert not session.reads


def test_missing_body_and_out_of_range_are_errors(corpus):
    for material in ["missing", "short"]:
        session = ToolSession(
            corpus, Task(role="deep_read", material_id=material, topic_ids=["learn"])
        )
        assert (
            session.execute(
                "material_read", json.dumps({"material_id": material, "start": 0, "length": 10})
            )["error"]
            == "body_unavailable"
        )
    session = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    assert (
        session.execute("material_read", '{"material_id":"tools","start":99999,"length":1}')[
            "error"
        ]
        == "range_out_of_bounds"
    )


def deep_final(coverage, limitations=""):
    return DeepFinal(
        status="succeeded",
        reason="已读取可用正文",
        deep_summary="分工与权限说明",
        coverage=coverage,
        limitations=limitations,
    )


def test_deep_read_requires_real_coverage_and_partial_disclosure(corpus):
    session = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    coverage = [{"material_id": "tools", "start": 0, "end": 10}]
    with pytest.raises(ValueError):
        session.validate_final(deep_final(coverage))
    session.execute("material_read", '{"material_id":"tools","start":0,"length":10}')
    with pytest.raises(ValueError):
        session.validate_final(deep_final(coverage))
    session.validate_final(deep_final(coverage, "只读前10字，不足以概括全文"))
    with pytest.raises(ValueError):
        session.validate_final(
            deep_final([{"material_id": "tools", "start": 0, "end": 11}], "部分")
        )


def test_full_deep_read_and_omitted_ranges(corpus):
    session = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    session.execute("material_read", '{"material_id":"tools","start":0,"length":4000}')
    session.validate_final(deep_final([r.model_dump() for r in session.reads]))
    session.execute("material_read", '{"material_id":"tools","start":10,"length":5}')
    # 重叠读取没有增加字符集合，报告全文范围仍然准确。
    session.validate_final(deep_final([session.reads[0].model_dump()]))
    with pytest.raises(ValueError):
        session.validate_final(
            deep_final(
                [{"material_id": "tools", "start": 0, "end": session.reads[0].end - 1}], "部分"
            )
        )


def test_context_keeps_all_guidelines_and_only_current_topic(corpus):
    task = Task(
        role="deep_read",
        material_id="tools",
        topic_ids=["learn"],
        short_summary="已有短摘要",
        deep_read_guideline="原始指引",
        tag_ids=["learn-tools"],
        question="额外要求",
    )
    messages = build_context(corpus, task)
    text = json.dumps(messages, ensure_ascii=False)
    for expected in ["已有短摘要", "原始指引", "额外要求", "模型请求、程序执行、结果回传"]:
        assert expected in text
    assert "Agent 工程可靠性" not in text
    assert corpus.materials["tools"].body not in text


def test_task_rejects_multiple_discussion_topics_and_invalid_ids(corpus):
    with pytest.raises(ValidationError):
        Task(
            role="discuss", material_id="tools", topic_ids=["learn", "engineering"], question="问题"
        )
    with pytest.raises(ValueError):
        build_context(corpus, Task(role="route", material_id="tools", topic_ids=["private"]))


def call(session, name, **arguments):
    return session.execute(name, json.dumps(arguments))


def discuss_tools(state):
    store, service, sessions, memory, created, tid, tm = state
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    task = Task(role="discuss", material_id=mid, topic_ids=[tid], question="讨论")
    context = Harness(None, service.corpus(mid, [tid]), memory=memory).prepare(task)
    return context.session


def test_registry_tools_grouped_by_domain_and_scoped(corpus):
    assert {name for name, spec in REGISTRY.items() if "consolidate" not in spec.roles} == {
        "material_search",
        "material_read",
        "memory_search",
        "memory_read",
        "session_search",
        "session_read",
        "read_file",
        "write_file",
        "edit_file",
        "bash",
        "search_tool",
    }
    for prefix in ("material", "memory", "session"):
        for name in (prefix + "_search", prefix + "_read"):
            spec = REGISTRY[name]
            assert (spec.run or spec.arun).__module__.endswith(".tool." + prefix)
    tools = ToolSession(
        corpus, Task(role="deep_read", material_id="injection", topic_ids=["learn"])
    )
    assert corpus.materials["injection"].body
    assert tools.names == ["material_read"]
    for name in ("read_body", "expand_history", "search_history", "write_file", "memory_read"):
        assert tools.execute(name, "{}")["error"] == "tool_not_allowed"


def test_material_search_grants_only_returned_matches_and_never_returns_deep_summary(corpus):
    corpus.history["extra"] = HistoricalMaterial(
        id="extra",
        topic_id="learn",
        material_id="old-eng",
        title="工具较早材料",
        short_summary="工具" * 400,
        deep_summary="不应该出现在检索结果",
        published_at="2020",
    )
    tools = ToolSession(
        corpus, Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题")
    )
    assert (
        call(tools, "material_read", material_id="old-learn", start=0, length=10)["status"]
        == "error"
    )
    assert (
        call(tools, "material_search", topic_id="engineering", query="工具", limit=5)["error"]
        == "topic_not_in_scope"
    )
    assert call(tools, "material_search", topic_id="learn", query="不存在", limit=5)["data"] == []
    result = call(tools, "material_search", topic_id="learn", query="工具", limit=1)
    assert result["truncated"] and result["data"][0]["material_id"] == "old-learn"
    assert len(result["data"][0]["preview"]) <= 240
    assert "deep_summary" not in result["data"][0]
    assert (
        call(tools, "material_read", material_id="old-learn", start=0, length=10)["status"]
        == "success"
    )
    assert (
        call(tools, "material_read", material_id="old-eng", start=0, length=10)["error"]
        == "material_not_in_scope"
    )


async def test_invalid_or_out_of_scope_material_read_never_calls_loader(corpus):
    loaded = []

    async def loader(mid):
        loaded.append(mid)
        return "不该获取的正文"

    corpus.body_loader = loader
    tools = ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))
    assert (
        await tools.aexecute("material_read", '{"material_id":"old-eng","start":0,"length":10}')
    )["error"] == "material_not_in_scope"
    assert (
        await tools.aexecute("material_read", '{"material_id":"tools","start":true,"length":10}')
    )["error"] == "invalid_arguments"
    assert loaded == []


async def test_session_search_marks_clipped_quotes_and_keeps_original_messages_out_of_preview(
    corpus,
):
    async def loader(*args, **kwargs):
        return [
            {
                "session_id": "old",
                "branch_id": "branch",
                "leaf_entry_id": "leaf",
                "topic_id": "learn",
                "title": "旧讨论",
                "recall_status": "summarized",
                "summary_error": None,
                "summary": "相关决定",
                "snippet": "短预览",
                "messages": [{"entry_id": "entry", "content": "只供服务读取的原始消息"}],
                "evidence": [{"entry_id": "entry", "quote": "引用" * 250}],
            }
        ]

    corpus.session_loader = loader
    session = ToolSession(
        corpus, Task(role="discuss", material_id="tools", topic_ids=["learn"], question="回忆")
    )
    response = await session.aexecute(
        "session_search", json.dumps({"query": "决定", "topic_id": None, "limit": 1})
    )
    row = response["data"][0]
    assert row["truncated"] and len(row["evidence"][0]["quote"]) == 300
    assert "messages" not in row and "只供服务读取" not in json.dumps(response, ensure_ascii=False)


async def test_memory_search_is_preview_and_memory_read_requires_granted_id(state):
    _, _, _, memory, _, tid, _ = state
    content = "学习背景 " + "前面的说明" * 100 + "只有展开才能取得的尾部"
    mid = memory.add(content, topic_id=tid)
    tools = discuss_tools(state)
    assert call(tools, "memory_read", memory_id=mid)["error"] == "memory_not_in_scope"
    result = call(tools, "memory_search", topic_id=tid, query="学习背景")
    row = result["data"][0]
    assert row["memory_id"] == mid and len(row["preview"]) == 240
    assert "content" not in row and "只有展开才能取得的尾部" not in json.dumps(
        result, ensure_ascii=False
    )
    detail = call(tools, "memory_read", memory_id=mid)
    assert detail["data"]["content"] == content and detail["data"]["category"] == "profile"
    assert detail["data"]["topic_id"] == tid and not tools.reads
    assert call(tools, "memory_read", memory_id="../../secret")["error"] == "memory_not_in_scope"
    assert (
        tools.execute("memory_read", json.dumps({"memory_id": mid, "path": "memory.md"}))["error"]
        == "invalid_arguments"
    )


@pytest.mark.parametrize("change", ["delete", "correct"])
async def test_memory_read_rechecks_source_after_search(state, change):
    _, _, _, memory, _, tid, _ = state
    mid = memory.add("用户学习Python", topic_id=tid)
    tools = discuss_tools(state)
    assert call(tools, "memory_search", topic_id=tid, query="Python")["data"]
    if change == "delete":
        memory.delete(mid)
    else:
        memory.correct(mid, "用户学习Go")
    assert call(tools, "memory_read", memory_id=mid)["error"] == "memory_not_found_in_scope"


async def test_memory_scope_filters_topic_but_allows_global_records(state):
    store, _, _, memory, _, tid, _ = state
    other = store.topic("其他")
    private = memory.add("Python 私有事实", topic_id=other)
    global_id = memory.add("Python 通用背景")
    tools = discuss_tools(state)
    assert (
        call(tools, "memory_search", topic_id=other, query="Python")["error"]
        == "topic_not_in_scope"
    )
    results = call(tools, "memory_search", topic_id=tid, query="Python")["data"]
    assert {r["memory_id"] for r in results} == {global_id}
    assert call(tools, "memory_read", memory_id=global_id)["data"]["scope"] == "global"
    assert call(tools, "memory_read", memory_id=private)["error"] == "memory_not_in_scope"


async def test_session_search_is_brief_and_read_expands_only_the_returned_branch(state):
    _, _, sessions, memory, created, _, _ = state
    eid = append(sessions, created["branch_id"], "Harness " + "历史说明" * 300 + "需要展开的结尾")
    append(sessions, created["branch_id"], "原始助手决定：先保存再提炼。", "assistant")
    memory.provider = ScriptedProvider(ProviderError("unavailable"))
    tools = discuss_tools(state)
    args = {
        "session_id": created["session_id"],
        "branch_id": created["branch_id"],
        "start": 0,
        "limit": 10,
    }
    assert call(tools, "session_read", **args)["error"] == "session_not_in_scope"
    result = await tools.aexecute(
        "session_search", json.dumps({"query": "Harness", "topic_id": None, "limit": 1})
    )
    row = result["data"][0]
    assert (
        "messages" not in row
        and len(row["snippet"]) <= 600
        and row["recall_status"] == "snippet_only"
    )
    detail = call(tools, "session_read", **args)
    assert detail["status"] == "success" and detail["data"]["messages"][0]["entry_id"] == eid
    assert detail["data"]["messages"][0]["content"].endswith("需要展开的结尾")
    assert detail["data"]["messages"][1]["role"] == "assistant" and not tools.reads
    assert (
        call(tools, "session_read", **{**args, "branch_id": "guessed"})["error"]
        == "session_not_in_scope"
    )
    assert call(tools, "session_read", **{**args, "start": 100})["error"] == "range_out_of_bounds"


async def test_session_read_detects_branch_changes_after_search(state):
    _, _, sessions, memory, created, _, _ = state
    append(sessions, created["branch_id"], "Harness 历史")
    memory.provider = ScriptedProvider(ProviderError("unavailable"))
    tools = discuss_tools(state)
    await tools.aexecute(
        "session_search", json.dumps({"query": "Harness", "topic_id": None, "limit": 1})
    )
    append(sessions, created["branch_id"], "另一轮追加", "assistant")
    response = call(
        tools,
        "session_read",
        session_id=created["session_id"],
        branch_id=created["branch_id"],
        start=0,
        limit=2,
    )
    assert response["error"] == "session_changed_or_deleted"


async def test_session_pagination_preserves_whole_messages_at_output_boundary(state):
    _, _, sessions, memory, created, _, _ = state
    texts = ["Harness " + "甲" * 7990, "乙" * 16000, "丙" * 1000]
    for index, text in enumerate(texts):
        append(sessions, created["branch_id"], text, "assistant" if index == 1 else "user")
    memory.provider = ScriptedProvider(ProviderError("unavailable"))
    tools = discuss_tools(state)
    await tools.aexecute(
        "session_search", json.dumps({"query": "Harness", "topic_id": None, "limit": 1})
    )
    collected, start = [], 0
    for _ in range(3):
        response = call(
            tools,
            "session_read",
            session_id=created["session_id"],
            branch_id=created["branch_id"],
            start=start,
            limit=10,
        )["data"]
        assert all(not e["content_truncated"] for e in response["messages"])
        collected.extend(e["content"] for e in response["messages"])
        start = response["next_start"]
    assert collected == texts and start == 3


async def test_loop_uses_memory_search_then_read_without_faking_material_coverage(state):
    store, service, _, memory, _, tid, tm = state
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    memory_id = memory.add("用户学习Python", topic_id=tid)

    def tool_turn(name, arguments, cid):
        return Turn(calls=[ToolCall(cid, name, json.dumps(arguments))], finish_reason="tool_calls")

    provider = ScriptedProvider(
        tool_turn("memory_search", {"topic_id": tid, "query": "Python"}, "m1"),
        tool_turn("memory_read", {"memory_id": memory_id}, "m2"),
        '{"status":"succeeded","reason":"依据用户背景","answer":"根据你的Python学习背景解释。"}',
    )
    report = await Harness(provider, service.corpus(mid, [tid]), memory=memory).run(
        Task(role="discuss", material_id=mid, topic_ids=[tid], question="解释")
    )
    assert report.status == "succeeded" and [t["name"] for t in report.tools] == [
        "memory_search",
        "memory_read",
    ]
    assert report.readings == [] and report.model_calls == 3


def deep_session(corpus):
    return ToolSession(corpus, Task(role="deep_read", material_id="tools", topic_ids=["learn"]))


def test_adjacent_read_chunks_can_be_reported_as_one_range(corpus):
    tools = deep_session(corpus)
    tools.execute("material_read", '{"material_id":"tools","start":0,"length":100}')
    tools.execute("material_read", '{"material_id":"tools","start":100,"length":100}')
    tools.validate_final(deep_final([{"material_id": "tools", "start": 0, "end": 200}], "部分正文"))


def test_merged_range_must_not_bridge_unread_gap(corpus):
    tools = deep_session(corpus)
    tools.execute("material_read", '{"material_id":"tools","start":0,"length":50}')
    tools.execute("material_read", '{"material_id":"tools","start":100,"length":50}')
    with pytest.raises(ValueError):
        tools.validate_final(deep_final([{"material_id": "tools", "start": 0, "end": 150}], "部分"))
