"""验证统一生命周期入口，而非仅对内部方法逐行断言。"""

import asyncio

import pytest
from conftest import ScriptedProvider

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import Task
from reading_agent.services.discussion import Discussion
from reading_agent.storage.database import Conflict

ANSWER = '{"status":"succeeded","reason":"完成","answer":"权限由程序检查。"}'


def task():
    return Task(role="discuss", material_id="tools", topic_ids=["learn"], question="解释权限")


class History:
    def context(self, branch_id, *, exclude_ids):
        assert branch_id == "branch" and exclude_ids == {"question"}
        return "稳定根", []


class Memory:
    last_extraction_error = None

    def __init__(self, log):
        self.log = log

    def attach(self, corpus, task):
        self.log.append("attach")

    async def extract(self, branch_id, question):
        self.log.append("extract")


async def test_events_stream_before_commit_and_memory_runs_after_commit(corpus):
    log = []
    events = []
    committing, release = asyncio.Event(), asyncio.Event()

    async def commit(report):
        assert report.status == "succeeded"
        assert any(e.kind == "text_delta" for e in events)
        log.append("commit_started")
        committing.set()
        await release.wait()
        log.append("committed")

    pending = asyncio.create_task(
        Harness(ScriptedProvider(ANSWER), corpus, memory=Memory(log), sessions=History()).run(
            task(),
            branch_id="branch",
            question_entry_id="question",
            commit=commit,
            on_event=events.append,
        )
    )
    await asyncio.wait_for(committing.wait(), 1)
    assert "extract" not in log
    assert events[-1].kind == "ended"  # Loop 完成与整个 Harness 完成是不同事件。
    release.set()
    report = await pending
    assert log == ["attach", "commit_started", "committed", "extract"]
    assert [e.kind for e in events][-2:] == ["memory_finished", "harness_ended"]
    assert report.memory_status == "completed"


async def test_commit_conflict_prevents_memory_and_successful_harness_end(corpus):
    log, events = [], []

    def commit(report):
        raise Conflict("leaf moved")

    with pytest.raises(Conflict):
        await Harness(ScriptedProvider(ANSWER), corpus, memory=Memory(log), sessions=History()).run(
            task(),
            branch_id="branch",
            question_entry_id="question",
            commit=commit,
            on_event=events.append,
        )
    assert log == ["attach"]
    assert events[-1].kind == "harness_failed"
    assert not any(e.kind == "harness_ended" for e in events)


async def test_failed_loop_can_save_incomplete_result_but_never_extracts(corpus):
    log = []
    provider = ScriptedProvider('{"status":"failed","reason":"资料不足","answer":""}')
    report = await Harness(provider, corpus, memory=Memory(log), sessions=History()).run(
        task(),
        branch_id="branch",
        question_entry_id="question",
        commit=lambda report: log.append("commit"),
    )
    assert report.status == "failed" and report.memory_status == "skipped"
    assert log == ["attach", "commit"]


@pytest.mark.parametrize("swallow", [False, True])
async def test_memory_failure_is_separate_from_committed_answer(corpus, swallow):
    log, events = [], []

    class BrokenMemory(Memory):
        async def extract(self, branch_id, question):
            if swallow:
                self.last_extraction_error = "ProviderError"
            else:
                raise OSError("private details must not be forwarded")

    report = await Harness(
        ScriptedProvider(ANSWER), corpus, memory=BrokenMemory(log), sessions=History()
    ).run(
        task(),
        branch_id="branch",
        question_entry_id="question",
        commit=lambda report: log.append("committed"),
        on_event=events.append,
    )
    assert report.status == "succeeded" and "committed" in log
    assert report.memory_status == "failed"
    assert report.memory_error == ("ProviderError" if swallow else "OSError")
    assert "private details" not in str(events)


async def test_cancel_during_memory_extraction_closes_work_and_keeps_answer(corpus):
    started, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingMemory(Memory):
        async def extract(self, branch_id, question):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

    pending = asyncio.create_task(
        Harness(
            ScriptedProvider(ANSWER), corpus, memory=BlockingMemory([]), sessions=History()
        ).run(
            task(),
            branch_id="branch",
            question_entry_id="question",
            commit=lambda report: None,
            cancel=cancel,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    cancel.set()
    report = await asyncio.wait_for(pending, 1)
    assert closed.is_set()
    assert report.status == "succeeded" and report.memory_status == "cancelled"


async def test_cancel_before_loop_never_extracts(corpus):
    log = []
    cancel = asyncio.Event()
    cancel.set()
    provider = ScriptedProvider()
    report = await Harness(provider, corpus, memory=Memory(log), sessions=History()).run(
        task(),
        branch_id="branch",
        question_entry_id="question",
        cancel=cancel,
        commit=lambda report: None,
    )
    assert report.end_reason == "cancelled" and provider.requests == []
    assert "extract" not in log


async def test_memory_without_commit_is_rejected_before_model_call(corpus):
    provider = ScriptedProvider()
    with pytest.raises(ValueError, match="explicit result commit"):
        await Harness(provider, corpus, memory=Memory([]), sessions=History()).run(
            task(), branch_id="branch", question_entry_id="question"
        )
    assert provider.requests == []


def test_prepare_snapshots_task_and_corpus_and_loads_history(corpus):
    original = task()
    seen = []

    def loader(prepared):
        seen.append(prepared.session_root)
        return corpus

    context = Harness(None, loader, sessions=History()).prepare(
        original, branch_id="branch", question_entry_id="question"
    )
    context.session.corpus.materials["tools"].body = "changed"
    assert corpus.materials["tools"].body != "changed"
    assert original.session_root != "稳定根"
    assert context.task.session_root == "稳定根" and seen == ["稳定根"]


async def test_discussion_commits_before_persisted_memory_extraction(state):
    store, service, sessions, memory, created, tid, tm = state
    service.provider = ScriptedProvider(
        ANSWER,
        '{"profiles":[{"content":"用户熟悉Java","quote":"我熟悉Java","explicit_global":false}]}',
    )
    memory.provider = service.provider
    bid = created["branch_id"]
    discussion = Discussion(store, sessions, service, memory)
    discussion.start(bid, "我熟悉Java", expected_leaf=sessions.path(bid)[-1]["id"])
    run = discussion.runs[bid]
    queue = asyncio.Queue(maxsize=128)
    run.subscribers.add(queue)
    await run.task
    assert run.phase == "completed", run.error
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    types = [e["type"] for e in events]
    assert types.index("message_saved") < types.index("memory_finished")
    assert sessions.path(bid)[-1]["data"]["content"] == "权限由程序检查。"
    assert len(service.provider.requests) == 2
    assert memory.last_extraction_error is None
    assert any("Java" in row["content"] for row in memory.list(tid))
