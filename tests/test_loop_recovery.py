import copy
import json

import pytest
from conftest import ScriptedProvider
from test_runtime import tool_turn

from reading_agent.agents.harness import Budget, Harness
from reading_agent.agents.messages import sanitize_messages
from reading_agent.agents.models import Task
from reading_agent.services.discussion import Discussion
from reading_agent.storage.database import Conflict, identity


def call(key, name="material_read", arguments="{}"):
    return {"id": key, "type": "function", "function": {"name": name, "arguments": arguments}}


def result(key, content='{"status":"success"}'):
    return {"role": "tool", "tool_call_id": key, "content": content}


def test_message_cleanup_preserves_pairs_and_failed_results_without_mutation():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": None, "tool_calls": [call("a"), call("b")]},
        result("a", '{"status":"error","error":"invalid_arguments"}'),
        result("orphan"),
    ]
    original = copy.deepcopy(messages)
    cleaned, count = sanitize_messages(messages)
    assert messages == original and count == 2
    assert [c["id"] for c in cleaned[2]["tool_calls"]] == ["a"]
    assert cleaned[-1]["content"].startswith('{"status":"error"')


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "assistant", "content": None, "tool_calls": [call("a")]},
            result("a"),
            result("a"),
        ],
        [{"role": "assistant", "content": None, "tool_calls": [call("a"), call("a")]}, result("a")],
        [
            {"role": "assistant", "content": None, "tool_calls": [call("a")]},
            {"role": "user", "content": "interrupted"},
            result("a"),
        ],
        [{"role": "assistant", "content": None, "tool_calls": 123}, result(["bad"])],
    ],
)
def test_ambiguous_and_interleaved_tool_messages_are_removed(messages):
    cleaned, removed = sanitize_messages(messages)
    assert removed and not any(
        message.get("role") == "tool" or message.get("tool_calls") for message in cleaned
    )


def test_bad_tool_arguments_remain_paired_so_model_can_repair_them():
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [call("a", arguments="{broken")]},
        result("a", '{"error":"invalid_arguments"}'),
    ]
    assert sanitize_messages(messages) == (messages, 0)


async def test_read_only_transient_failure_retries_and_consumes_tool_budget(corpus):
    attempts = 0

    async def loader(mid):
        nonlocal attempts
        import httpx

        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary")
        return "body"

    corpus.materials["tools"].body = None
    corpus.body_loader = loader
    provider = ScriptedProvider(tool_turn(), '{"status":"succeeded","reason":"ok","answer":"ok"}')
    events = []
    report = await Harness(provider, corpus, Budget(retry_delay=0)).run(
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="read"),
        on_event=events.append,
    )
    assert report.status == "succeeded" and report.tool_calls == 2 and attempts == 2
    assert any(event.kind == "tool_retry_scheduled" for event in events)
    assert report.tools[0]["result"]["status"] == "success"


class Crash(Exception):
    pass


async def save_interrupted(state, *, after_result=True, target="write_file", steps=None):
    store, service, sessions, _, created, tid, tm = state
    bid = created["branch_id"]
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    question = "save a note"
    leaf = sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": "user", "content": question})]
    )
    run_id = identity()
    sessions.record_event(bid, run_id, "run_started", {"question_entry_id": leaf})
    if steps is None:
        steps = [
            tool_turn("search_tool", {"query": "write_file", "limit": 1}, "search"),
            tool_turn(
                "write_file",
                {"path": "note.txt", "content": "hello", "expected_sha256": None},
                "write",
            ),
        ]
    provider = ScriptedProvider(*steps)

    def observe(event):
        crash = event.kind == "tool_result" and event.data["name"] == target
        if crash and not after_result:
            raise Crash("before result persisted")
        sessions.record_event(bid, run_id, event.kind, event.data)
        if crash:
            raise Crash("after result persisted")

    with pytest.raises(Crash):
        await Harness(
            provider,
            lambda task: service.corpus(task.material_id, task.topic_ids, persist=True),
            Budget(max_model_calls=10, max_seconds=30),
            sessions=sessions,
        ).run(
            Task(role="discuss", material_id=mid, topic_ids=[tid], question=question),
            branch_id=bid,
            question_entry_id=leaf,
            on_event=observe,
        )
    sessions.interrupt_runs()
    return bid, run_id, service.corpus(mid, [tid], persist=True).workspace


@pytest.mark.parametrize("after_result", [False, True])
async def test_resume_never_writes_completed_file_twice(state, monkeypatch, after_result):
    from reading_agent.infrastructure.workspace import Workspace

    writes = []
    original = Workspace.write

    def write(self, *args):
        writes.append(args[0])
        return original(self, *args)

    monkeypatch.setattr(Workspace, "write", write)
    bid, old, workspace = await save_interrupted(state, after_result=after_result)
    store, service, sessions, *_ = state
    assert (workspace.root / "note.txt").read_text() == "hello" and len(writes) == 1
    service.provider = ScriptedProvider(
        tool_turn(
            "write_file",
            {"expected_sha256": None, "content": "hello", "path": "note.txt"},
            "repeated",
        ),
        '{"status":"succeeded","reason":"ok","answer":"saved"}',
    )
    discussion = Discussion(store, sessions, service)
    assert discussion.recovery_info(bid)["can_resume"]
    discussion.resume(bid, old)
    await discussion.runs[bid].task
    assert discussion.runs[bid].phase == "completed" and len(writes) == 1
    assert discussion.runs[bid].report.tools[-1]["result"]["replayed_result"]
    assert discussion.runs[bid].report.model_calls == 4
    assert not discussion.recovery_info(bid)["can_resume"]


async def test_recovery_rejects_changed_file_and_changed_branch(state):
    bid, old, workspace = await save_interrupted(state)
    store, service, sessions, *_ = state
    (workspace.root / "note.txt").write_text("user changed")
    provider = ScriptedProvider()
    service.provider = provider
    discussion = Discussion(store, sessions, service)
    discussion.resume(bid, old)
    await discussion.runs[bid].task
    assert discussion.runs[bid].report.end_reason == "recovery_file_version_changed"
    assert not provider.requests


async def test_unknown_bash_is_not_reexecuted_or_reapproved(state):
    steps = [
        tool_turn("search_tool", {"query": "bash", "limit": 1}, "search"),
        tool_turn("bash", {"command": "echo pending", "timeout_seconds": 1}, "shell"),
    ]
    bid, old, _ = await save_interrupted(state, after_result=False, target="bash", steps=steps)
    store, service, sessions, *_ = state
    service.provider = ScriptedProvider(
        tool_turn("bash", {"command": "echo different", "timeout_seconds": 1}, "new-shell"),
        '{"status":"succeeded","reason":"unknown execution","answer":"please reconcile"}',
    )
    discussion = Discussion(store, sessions, service)
    assert discussion.recovery_info(bid)["unknown_operations"][0]["name"] == "bash"
    discussion.resume(bid, old)
    await discussion.runs[bid].task
    assert (
        discussion.runs[bid].report.tools[-1]["result"]["error"]
        == "previous_execution_unknown_requires_reconciliation"
    )
    assert discussion.runs[bid].phase == "completed"


@pytest.mark.parametrize("change", ["stopped", "advanced"])
async def test_stopped_and_advanced_branches_cannot_resume(state, change):
    bid, old, _ = await save_interrupted(state)
    store, service, sessions, *_ = state
    if change == "stopped":
        sessions.record_event(bid, old, "run_finished", {"status": "stopped", "error": "cancelled"})
    else:
        sessions.append(
            bid,
            sessions.path(bid)[-1]["id"],
            [("message", {"role": "user", "content": "new question"})],
        )
    discussion = Discussion(store, sessions, service)
    assert not discussion.recovery_info(bid)["can_resume"]
    with pytest.raises(Conflict):
        discussion.resume(bid, old)


async def test_resume_budget_is_not_reset_and_permissions_rechecked(state, monkeypatch):
    from reading_agent.agents.permission import PermissionPolicy

    bid, old, _ = await save_interrupted(state)
    store, service, sessions, *_ = state
    original = service.corpus

    def restricted(*args, **kwargs):
        corpus = original(*args, **kwargs)
        corpus.permission_policy = PermissionPolicy(level="read")
        return corpus

    monkeypatch.setattr(service, "corpus", restricted)
    service.provider = ScriptedProvider()
    discussion = Discussion(store, sessions, service)
    discussion.resume(bid, old)
    with pytest.raises(Conflict):
        discussion.resume(bid, old)
    await discussion.runs[bid].task
    assert discussion.runs[bid].report.end_reason == "recovery_permissions_changed"
    assert not service.provider.requests


async def test_completed_final_checkpoint_commits_without_another_model_call(state):
    store, service, sessions, _, created, tid, tm = state
    bid = created["branch_id"]
    mid = store.one("SELECT material_id FROM topic_materials WHERE id=?", (tm,))["material_id"]
    leaf = sessions.append(
        bid, sessions.path(bid)[-1]["id"], [("message", {"role": "user", "content": "question"})]
    )
    run_id = identity()
    sessions.record_event(bid, run_id, "run_started", {"question_entry_id": leaf})

    def observe(event):
        sessions.record_event(bid, run_id, event.kind, event.data)
        if event.kind == "checkpoint" and event.data["state"]["report"]["status"] == "succeeded":
            raise Crash()

    provider = ScriptedProvider(
        '{"status":"succeeded","reason":"ok","answer":"ready before crash"}'
    )
    with pytest.raises(Crash):
        await Harness(
            provider,
            lambda task: service.corpus(task.material_id, task.topic_ids, persist=True),
            sessions=sessions,
        ).run(
            Task(role="discuss", material_id=mid, topic_ids=[tid], question="question"),
            branch_id=bid,
            question_entry_id=leaf,
            on_event=observe,
        )
    sessions.interrupt_runs()
    service.provider = ScriptedProvider()
    discussion = Discussion(store, sessions, service)
    discussion.resume(bid, run_id)
    await discussion.runs[bid].task
    assert discussion.runs[bid].phase == "completed" and not service.provider.requests
    assert sessions.path(bid)[-1]["data"]["content"] == "ready before crash"


async def test_resume_respects_already_consumed_model_budget(state):
    bid, old, _ = await save_interrupted(state)
    store, service, sessions, *_ = state
    info = sessions.recovery_state(bid)
    saved = info["state"]
    saved["budget"]["max_model_calls"] = saved["report"]["model_calls"]
    sessions.record_event(bid, old, "checkpoint", {"state": saved})
    service.provider = ScriptedProvider()
    discussion = Discussion(store, sessions, service)
    discussion.resume(bid, old)
    await discussion.runs[bid].task
    assert (
        discussion.runs[bid].report.end_reason == "model_budget_exhausted"
        and not service.provider.requests
    )


@pytest.mark.parametrize("changed", [False, True])
async def test_generic_deep_loop_restores_verified_reading_receipts(corpus, changed):
    saved = None
    task = Task(
        role="deep_read", material_id="tools", topic_ids=["learn"], deep_read_guideline="核实前10字"
    )

    def observe(event):
        nonlocal saved
        if event.kind == "checkpoint" and event.data["state"]["reads"]:
            saved = copy.deepcopy(event.data["state"])
            raise Crash()

    with pytest.raises(Crash):
        await Harness(
            ScriptedProvider(
                tool_turn(arguments={"material_id": "tools", "start": 0, "length": 10})
            ),
            corpus,
        ).run(task, on_event=observe)
    assert saved is not None
    if changed:
        corpus.materials["tools"].body = "changed source"
    final = json.dumps(
        {
            "status": "succeeded",
            "reason": "实际已读",
            "deep_summary": "基于前10字的有限说明",
            "coverage": [{"material_id": "tools", "start": 0, "end": 10}],
            "limitations": "只读取前10字，不能代表全文",
        }
    )
    provider = ScriptedProvider(final)
    report = await Harness(provider, corpus).run(task, recovery=saved)
    assert report.end_reason == ("recovery_material_version_changed" if changed else "completed")
    assert len(provider.requests) == (0 if changed else 1)
    if not changed:
        assert report.model_calls == 2 and report.tool_calls == 1 and report.readings


async def test_permanent_tool_error_is_returned_without_automatic_retry(corpus):
    provider = ScriptedProvider(
        tool_turn(arguments={"material_id": "outside", "start": 0, "length": 1}),
        '{"status":"succeeded","reason":"denied","answer":"没有权限读取"}',
    )
    report = await Harness(provider, corpus).run(
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="test")
    )
    assert report.tool_calls == 1 and report.tools[0]["result"]["error"] == "material_not_in_scope"


async def test_corrupt_checkpoint_is_not_advertised_as_resumable(state):
    bid, old, _ = await save_interrupted(state)
    _, _, sessions, *_ = state
    saved = sessions.recovery_state(bid)["state"]
    saved["report"]["model_calls"] = -1
    sessions.record_event(bid, old, "checkpoint", {"state": saved})
    assert sessions.recovery_state(bid) == {"can_resume": False, "reason": "checkpoint_invalid"}
