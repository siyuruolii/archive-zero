import asyncio
import json
import os
import sys

import pytest

from reading_agent.agents.harness import Harness
from reading_agent.agents.models import Task
from reading_agent.agents.tool import ToolSession
from reading_agent.agents.tool.shell import container_arguments, execute_container
from reading_agent.infrastructure.workspace import Workspace


def tools(corpus, tmp_path):
    corpus.workspace = Workspace(tmp_path / "workspace", "sha256:" + "a" * 64)
    session = ToolSession(
        corpus,
        Task(role="discuss", material_id="tools", topic_ids=["learn"], question="test tools"),
    )
    session.loaded_tools.update(session.eligible_names)
    return session


def call(session, name, **args):
    return session.execute(name, json.dumps(args))


def test_file_roundtrip_edit_and_stale_read_cache(corpus, tmp_path):
    session = tools(corpus, tmp_path)
    result = call(
        session, "write_file", path="notes/a.md", content="中文初稿😀", expected_sha256=None
    )
    assert result["status"] == "success"
    digest = result["data"]["sha256"]
    read = call(session, "read_file", path="notes/a.md", offset=0, length=2)
    assert read["data"]["text"] == "中文" and read["data"]["truncated"]
    assert (
        call(
            session,
            "edit_file",
            path="notes/a.md",
            old_text="初稿",
            new_text="修订",
            expected_sha256=digest,
        )["status"]
        == "success"
    )
    read = call(session, "read_file", path="notes/a.md", offset=0, length=100)
    assert read["data"]["text"] == "中文修订😀"  # Same arguments must not reuse stale content.
    assert (
        call(session, "write_file", path="notes/a.md", content="stale", expected_sha256=digest)[
            "error"
        ]
        == "file_version_conflict"
    )
    assert (
        call(session, "write_file", path="notes/a.md", content="overwrite", expected_sha256=None)[
            "error"
        ]
        == "file_version_conflict"
    )


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/tmp/a",
        "C:/secret",
        r"..\outside",
        ".env",
        "x/.local/y",
        "NUL",
        "a/COM1.txt",
        "a:stream",
        "a./b",
        "a /b",
        "//server/share",
        "a\x00b",
    ],
)
def test_file_path_escape_and_windows_special_names(corpus, tmp_path, path):
    session = tools(corpus, tmp_path)
    assert (
        call(session, "write_file", path=path, content="bad", expected_sha256=None)["status"]
        == "error"
    )
    assert not (tmp_path / "outside").exists()


def test_directory_listing_and_workspace_quota(corpus, tmp_path):
    session = tools(corpus, tmp_path)
    result = call(session, "write_file", path="notes/a.txt", content="test", expected_sha256=None)
    assert result["status"] == "success"
    listing = call(session, "read_file", path=".", offset=0, length=100)
    assert listing["data"]["entries"] == ["notes"]
    for index in range(15):
        assert (
            call(
                session,
                "write_file",
                path=f"part-{index}",
                content="x" * 262144,
                expected_sha256=None,
            )["status"]
            == "success"
        )
    assert (
        call(
            session, "write_file", path="too-big-total", content="x" * 262144, expected_sha256=None
        )["error"]
        == "workspace_size_limit"
    )


def test_link_and_binary_refusal(corpus, tmp_path):
    session = tools(corpus, tmp_path)
    outside = tmp_path / "secret"
    outside.write_text("private")
    target = session.corpus.workspace.root / "linked"
    os.link(outside, target)
    assert (
        call(session, "read_file", path="linked", offset=0, length=10)["error"]
        == "regular_single_link_file_required"
    )
    assert (
        call(session, "write_file", path="linked", content="bad", expected_sha256=None)["status"]
        == "error"
    )
    assert outside.read_text() == "private"
    target.unlink()
    target.write_bytes(b"\x00binary")
    assert call(session, "read_file", path="linked", offset=0, length=10)["status"] == "error"


def test_symlink_refused(corpus, tmp_path):
    session = tools(corpus, tmp_path)
    try:
        (session.corpus.workspace.root / "link").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not grant symlink creation; hardlink test still runs")
    assert (
        call(session, "write_file", path="link/outside", content="bad", expected_sha256=None)[
            "error"
        ]
        == "links_not_allowed"
    )


def test_edit_ambiguity_and_atomic_failure(corpus, tmp_path, monkeypatch):
    session = tools(corpus, tmp_path)
    digest = call(session, "write_file", path="a.txt", content="same same", expected_sha256=None)[
        "data"
    ]["sha256"]
    assert (
        call(
            session,
            "edit_file",
            path="a.txt",
            old_text="same",
            new_text="x",
            expected_sha256=digest,
        )["error"]
        == "edit_requires_unique_match"
    )

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr("reading_agent.infrastructure.workspace.os.replace", fail)
    assert (
        call(session, "write_file", path="a.txt", content="new", expected_sha256=digest)["status"]
        == "error"
    )
    assert (session.corpus.workspace.root / "a.txt").read_text() == "same same"
    assert not list(session.corpus.workspace.root.glob(".write-*"))


def test_file_size_and_lock_limits(corpus, tmp_path):
    session = tools(corpus, tmp_path)
    assert (
        call(session, "write_file", path="big", content="汉" * 100000, expected_sha256=None)[
            "error"
        ]
        == "file_size_or_format_limit"
    )
    with session.corpus.workspace.lock():
        assert (
            call(session, "write_file", path="a", content="x", expected_sha256=None)["error"]
            == "workspace_busy"
        )


def test_scoped_schemas_and_harness_prompt(corpus, tmp_path):
    task = Task(role="discuss", material_id="tools", topic_ids=["learn"], question="test tools")
    assert "bash" not in ToolSession(corpus, task).names
    session = tools(corpus, tmp_path)
    assert {"read_file", "write_file", "edit_file", "bash"} <= set(session.names)
    assert (
        "write_file"
        not in ToolSession(
            corpus, Task(role="route", material_id="tools", topic_ids=["learn"])
        ).names
    )
    prepared = Harness(None, corpus).prepare(task)
    assert "isolated_task_workspace" in prepared.messages[0]["content"]
    assert "no filesystem or command tools" not in prepared.messages[-1]["content"]


def test_container_isolation_contract(tmp_path):
    workspace = Workspace(tmp_path / "work", "sha256:" + "a" * 64)
    args = container_arguments(workspace, "trusted-name", "echo hello")
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--pull=never",
        "--log-driver=none",
    ):
        assert flag in args
    assert not any(str(workspace.root) in arg for arg in args)
    assert "--mount" not in args and "-v" not in args and "--privileged" not in args
    with pytest.raises(ValueError):
        container_arguments(Workspace(tmp_path / "other", "--privileged"), "n", "x")


async def test_bash_missing_runtime_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr("reading_agent.agents.tool.shell.shutil.which", lambda _: None)
    assert (await execute_container(Workspace(tmp_path / "w"), "echo unsafe", 1))[
        "error"
    ] == "sandbox_runtime_unavailable"


@pytest.mark.parametrize(
    "scenario", ["success", "nonzero", "output", "timeout", "cancel", "cleanup"]
)
async def test_bash_lifecycle_using_real_controlled_child_processes(
    tmp_path, monkeypatch, scenario
):
    monkeypatch.setattr("reading_agent.agents.tool.shell.shutil.which", lambda _: sys.executable)
    monkeypatch.setenv("AGENT_API_KEY", "must-not-inherit")
    commands, processes = [], []
    started = asyncio.Event()

    async def spawn(executable, *args, **options):
        commands.append(args)
        assert "AGENT_API_KEY" not in options["env"]
        if args[0] == "rm":
            code = "raise SystemExit(1)" if scenario == "cleanup" else "pass"
        else:
            code = {
                "success": 'print("ok")',
                "nonzero": "raise SystemExit(7)",
                "output": 'print("x"*50000)',
                "timeout": "import time;time.sleep(30)",
                "cancel": "import time;time.sleep(30)",
                "cleanup": 'print("ok")',
            }[scenario]
        process = await asyncio.create_subprocess_exec(executable, "-c", code, **options)
        processes.append(process)
        if args[0] == "run":
            started.set()
        return process

    work = asyncio.create_task(
        execute_container(
            Workspace(tmp_path / "w", "sha256:" + "a" * 64),
            "untrusted command",
            0.1 if scenario == "timeout" else 5,
            spawn=spawn,
        )
    )
    await started.wait()
    if scenario == "cancel":
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        result = await work
        expected = {
            "success": None,
            "nonzero": "command_failed",
            "output": "command_output_limit",
            "timeout": "command_timeout",
            "cleanup": "sandbox_cleanup_unconfirmed",
        }[scenario]
        assert result["error"] == expected
        if result.get("data"):
            assert len(result["data"]["stdout"]) <= 32768
    assert commands[-1][:3] == ("rm", "-f", "-v")
    assert all(p.returncode is not None for p in processes)
    assert bool(list((tmp_path / "w").glob(".sandbox-*.json"))) == (scenario == "cleanup")


@pytest.mark.skipif(
    not os.environ.get("AGENT_TEST_CONTAINER_IMAGE"),
    reason="No trusted local Docker image configured; real container acceptance not run",
)
async def test_real_container_execution_and_isolation(tmp_path):
    workspace = Workspace(tmp_path / "real-container", os.environ["AGENT_TEST_CONTAINER_IMAGE"])
    result = await execute_container(
        workspace,
        'printf "value=%s\\n" "$((6 * 7))"; test ! -e /workspace; test -z "$AGENT_API_KEY"',
        15,
    )
    assert result["status"] == "success" and "value=42" in result["data"]["stdout"]
    assert result["data"]["cleanup_confirmed"]
