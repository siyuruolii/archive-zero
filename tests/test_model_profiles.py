"""阶段选模、接口能力、缓存观测与设置的隔离集成验证。"""

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import route_json
from test_compact import SUMMARY, answer, discussion_task
from test_provider import frame

from reading_agent.agents.harness import Harness
from reading_agent.agents.model_router import STAGES, ModelCatalog, ModelRouter, stage_provider
from reading_agent.agents.models import RouteFinal, Task
from reading_agent.agents.prompts import RuntimeEnvironment, build_prompt_document
from reading_agent.agents.provider import ChatProvider, ModelRequest, Settings
from reading_agent.infrastructure import configuration as config
from reading_agent.interfaces.api import create_app


def settings(model="main", **changes):
    return Settings(
        **(
            dict(api_key="isolated-test-secret", model=model, base_url="https://model.test/v1")
            | changes
        )
    )


def catalog(**stages):
    return ModelCatalog(
        default="main", profiles={"main": settings(), "small": settings("small")}, stages=stages
    )


def response(text, usage=None):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=frame({"content": text}, "stop", usage) + "data: [DONE]\n\n",
    )


@pytest.mark.parametrize("stage", STAGES)
async def test_each_stage_resolves_and_inherits_without_vendor_binding(stage):
    async with httpx.AsyncClient() as client:
        router = ModelRouter(catalog(**{stage: "small"}), client)
        assert stage_provider(router, stage).model_name == "small"
        other = next(s for s in STAGES if s != stage)
        assert stage_provider(router, other).model_name == "main"
        provider = router.for_stage(stage)
        assert stage_provider(provider, other) is provider


async def test_compaction_uses_its_model_and_shared_loop_budget(corpus):
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        if payload["model"] == "small":
            return response(
                SUMMARY, {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 60}}
            )
        return response(answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await Harness(ModelRouter(catalog(compact="small"), client), corpus).run(
            discussion_task()
        )
    assert report.status == "succeeded" and report.model_calls == 3
    assert [r["model"] for r in calls] == ["main", "small", "main"]
    assert [r["model"] for r in report.attempts] == ["main", "small", "main"]
    assert report.attempts[1]["cache_hit_tokens"] == 60
    assert report.attempts[1]["cache_miss_tokens"] == 40
    assert report.attempts[-1]["cache_status"] == "unknown"


async def test_session_recall_uses_aux_model_inside_same_loop_budget(corpus):
    calls = []

    async def load(query, *, topic_id, limit, summarize):
        await summarize(RouteFinal, "仅输出JSON", {})
        return []

    corpus.session_loader = load

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "recall",
                        "type": "function",
                        "function": {
                            "name": "session_search",
                            "arguments": json.dumps(
                                {"query": "历史", "topic_id": "learn", "limit": 1}
                            ),
                        },
                    }
                ]
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=frame(delta, "tool_calls") + "data: [DONE]\n\n",
            )
        return response(route_json() if payload["model"] == "small" else answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        report = await Harness(ModelRouter(catalog(session_recall="small"), client), corpus).run(
            Task(role="discuss", material_id="tools", topic_ids=["learn"], question="回忆历史")
        )
    assert report.status == "succeeded" and report.model_calls == 3
    assert [c["model"] for c in calls] == ["main", "small", "main"]
    assert [a["purpose"] for a in report.attempts] == ["agent", "session_recall", "agent"]


async def test_auxiliary_inference_uses_selected_model():
    stage = "extract"  # 所有阶段映射由 test_each_stage_resolves... 验证。
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return response(route_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await Harness(ModelRouter(catalog(**{stage: "small"}), client)).infer(
            RouteFinal, "仅输出JSON", {}, stage=stage
        )
    assert result.status == "succeeded" and calls[0]["model"] == "small"


@pytest.mark.parametrize("json_mode", ["text", "json_object", "json_schema"])
async def test_adapter_output_modes_and_strict_tool_capabilities(corpus, json_mode):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return response(route_json())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = ChatProvider(
            settings(
                json_mode=json_mode,
                strict_tools=False,
                reasoning_effort="low",
                token_field="max_completion_tokens",
            ),
            client,
        )
        result = await Harness(provider, corpus).run(
            Task(role="route", material_id="tools", topic_ids=["learn"])
        )
    assert result.status == "succeeded"
    payload = calls[0]
    assert payload["reasoning_effort"] == "low" and payload["max_completion_tokens"] == 4096
    assert all("strict" not in t["function"] for t in payload["tools"])
    assert "cache_mode" not in payload and "api_key" not in payload
    if json_mode == "text":
        assert "response_format" not in payload
    else:
        assert payload["response_format"]["type"] == json_mode
        if json_mode == "json_schema":
            assert payload["response_format"]["json_schema"]["strict"] is True


async def test_unsupported_tools_fail_before_http(corpus):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("must not call HTTP"))
    ) as client:
        report = await Harness(ChatProvider(settings(supports_tools=False), client), corpus).run(
            Task(role="route", material_id="tools", topic_ids=["learn"])
        )
    assert report.end_reason == "provider_tools_unsupported"


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60},
            (40, 60),
        ),
        ({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}, (0, 100)),
        ({"prompt_tokens": 100}, (None, None)),
    ],
)
async def test_cache_statistics_distinguish_zero_from_unknown(usage, expected):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response(route_json(), usage))
    ) as client:
        provider = ChatProvider(settings(), client)
        events = [
            e
            async for e in provider.stream(
                ModelRequest(
                    [{"role": "user", "content": "JSON"}], [], RouteFinal.model_json_schema(), 100
                )
            )
        ]
        turn = events[-1].turn
    hit = turn.usage.get("prompt_cache_hit_tokens")
    assert hit == expected[0]
    stats = provider.statistics
    assert stats["completed"] == 1
    assert stats["cache_unknown_calls"] == (expected[0] is None)
    if expected[0] is not None:
        assert (stats["cache_hit_tokens"], stats["cache_miss_tokens"]) == expected


def test_stable_prompt_ignores_clock_material_id_and_memory_refresh(corpus):
    task = Task(role="discuss", material_id="tools", topic_ids=["learn"], question="问题")
    first = Harness(
        None,
        corpus,
        runtime=RuntimeEnvironment(captured_at=datetime(2026, 1, 1, tzinfo=UTC)),
    ).prepare(task)
    corpus.memories = {"learn": [{"content": "changed memory"}]}
    corpus.materials["tools"].title = "changed title"
    second = Harness(
        None,
        corpus,
        runtime=RuntimeEnvironment(captured_at=datetime(2026, 1, 2, tzinfo=UTC)),
    ).prepare(task)
    assert first.messages[:2] == second.messages[:2]
    assert first.messages[-1] != second.messages[-1]
    doc = build_prompt_document(first.session)
    assert doc.render() == first.messages[0]["content"]
    assert "2026-01-01" not in doc.render()


async def test_persisted_turn_input_replays_exactly_after_memory_and_clock_change(state):
    store, service, sessions, memory, created, tid, tm = state
    bid = created["branch_id"]
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    question = "第一问带尾随空白 \n"
    eid = sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": "user", "content": question})]
    )
    task = Task(role="discuss", material_id=mid, topic_ids=[tid], question=question)
    first = Harness(None, service.corpus(mid, [tid]), memory=memory, sessions=sessions).prepare(
        task, branch_id=bid, question_entry_id=eid, result_commit=True
    )
    original = first.messages[-1]
    sessions.append(bid, eid, [("message", {"role": "assistant", "content": "回答"})])
    memory.add("新的明确偏好", topic_id=tid)
    q2 = sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": "user", "content": "第二问"})]
    )
    task2 = task.model_copy(update={"question": "第二问"})
    second = Harness(None, service.corpus(mid, [tid]), memory=memory, sessions=sessions).prepare(
        task2, branch_id=bid, question_entry_id=q2, result_commit=True
    )
    assert second.messages[: len(first.messages)] == first.messages
    assert second.messages[2] == original
    assert original["content"].endswith(question)
    assert sessions.path(bid)[1]["data"]["content"] == question
    assert "model_input" not in json.loads(
        store.one("SELECT data FROM entries WHERE id=?", (eid,))["data"]
    )
    sessions.rebuild_search_index()
    assert "本轮上下文快照" not in str(memory.search_sessions("第一问"))


async def test_prompt_snapshot_rejects_another_branch_and_reuses_first_capture(state):
    from reading_agent.storage.database import Conflict

    _, _, sessions, _, created, _, tm = state
    bid = created["branch_id"]
    root = sessions.path(bid)[-1]["id"]
    other = sessions.fork(bid, root)
    eid = sessions.append(bid, root, [("message", {"role": "user", "content": "问题"})])
    with pytest.raises(Conflict, match="branch leaf"):
        sessions.prepare_model_input(eid, "问题", "snapshot", branch_id=other)
    assert sessions.prepare_model_input(eid, "问题", "snapshot", branch_id=bid) == "snapshot"
    assert sessions.prepare_model_input(eid, "问题", "new snapshot", branch_id=bid) == "snapshot"


def test_catalog_secret_storage_and_blank_key_reuse(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    value = catalog(extract="small")
    config.save_catalog(value, tmp_path)
    disk = (tmp_path / "models.json").read_text()
    assert "isolated-test-secret" not in disk and "api_key" not in disk
    loaded = config.load_catalog(tmp_path)
    assert loaded.profiles["small"].api_key.get_secret_value() == "isolated-test-secret"
    public = config.public_catalog(loaded)
    assert "isolated-test-secret" not in json.dumps(public)
    assert config.catalog_from_input(public, loaded).stages == {"extract": "small"}
    public["profiles"]["new"] = {**public["profiles"]["small"]}
    with pytest.raises(ValueError, match="invalid model settings"):
        config.catalog_from_input(public, loaded)


async def test_settings_api_auth_safe_validation_and_failed_save_preserves_config(
    tmp_path, monkeypatch
):
    import reading_agent.interfaces.api as api

    async with httpx.AsyncClient() as model_client:
        app = create_app(
            directory=tmp_path, token="token", provider=ChatProvider(settings(), model_client)
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://local"
            ) as client:
                assert (await client.get("/api/settings/models")).status_code == 401
                client.headers["authorization"] = "Bearer token"
                body = (await client.get("/api/settings/models")).json()
                assert "api_key" not in body["profiles"]["default"]
                body["profiles"]["small"] = settings("small").model_dump(mode="json") | {
                    "api_key": "isolated-test-secret"
                }
                body["stages"] = {"extract": "small"}
                saved = await client.put("/api/settings/models", json=body)
                assert saved.status_code == 200 and "isolated-test-secret" not in saved.text
                before = (tmp_path / "models.json").read_bytes()
                bad = await client.put(
                    "/api/settings/models", json=[{"api_key": "isolated-test-secret"}]
                )
                assert bad.status_code == 422 and "isolated-test-secret" not in bad.text
                body["stages"]["extract"] = "missing"
                rejected = await client.put("/api/settings/models", json=body)
                assert rejected.status_code == 400 and "isolated-test-secret" not in rejected.text
                assert (tmp_path / "models.json").read_bytes() == before
                body["stages"]["extract"] = "small"
                monkeypatch.setattr(
                    api,
                    "save_catalog",
                    lambda *a: (_ for _ in ()).throw(ValueError("injected disk failure")),
                )
                body["profiles"]["default"]["model"] = "must-not-activate"
                assert (await client.put("/api/settings/models", json=body)).status_code == 400
                assert app.state.service.provider.for_stage("route").model_name == "main"
                usage = (await client.get("/api/settings/model-usage")).json()["profiles"][
                    "default"
                ]
                assert usage["cache_hit_tokens"] is None and usage["cache_miss_tokens"] is None


async def test_profile_budget_stops_oversized_request_before_http(corpus):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("over budget"))
    ) as client:
        provider = ChatProvider(settings(context_byte_budget=1000, output_token_budget=32), client)
        report = await Harness(provider, corpus).run(
            Task(role="route", material_id="tools", topic_ids=["learn"])
        )
    assert report.end_reason == "context_budget_exhausted" and provider.statistics["requests"] == 0
