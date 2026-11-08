import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from reading_agent.agents.permission import PermissionPolicy, check_permission
from reading_agent.agents.tool.filesystem import ReadArgs, WriteArgs
from reading_agent.agents.tool.shell import BashArgs, execute_local_backend, select_backend
from reading_agent.infrastructure.processes import run_process
from reading_agent.infrastructure.workspace import Workspace


def test_sensitive_denies_precede_config_and_level():
    policy = PermissionPolicy(level="read", allowed_tools=["write_file"])
    args = WriteArgs(path=".ssh/id_rsa", content="x", expected_sha256=None)
    assert check_permission("write_file", args, policy) == "sensitive_path_denied"
    policy = PermissionPolicy(allowed_tools=["read_file"], denied_paths=["notes/private*"])
    assert (
        check_permission(
            "read_file", ReadArgs(path="notes/private.md", offset=0, length=10), policy
        )
        == "configured_path_denied"
    )
    assert (
        check_permission("bash", BashArgs(command="cat ~/.ssh/id_rsa", timeout_seconds=1))
        == "sensitive_path_denied"
    )
    assert (
        check_permission(
            "write_file",
            WriteArgs(path="a", content="x", expected_sha256=None),
            PermissionPolicy(level="read"),
        )
        == "permission_level_denied"
    )
    assert (
        check_permission(
            "bash",
            BashArgs(command="echo safe", timeout_seconds=1),
            PermissionPolicy(level="write"),
        )
        == "permission_level_denied"
    )
    assert (
        check_permission(
            "read_file",
            ReadArgs(path="a", offset=0, length=10),
            PermissionPolicy(allowed_tools=["read_file"], denied_tools=["read_file"]),
        )
        == "configured_tool_denied"
    )


async def test_backend_order_and_disabled_fallback(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "work", "sha256:" + "a" * 64)
    monkeypatch.setattr("reading_agent.agents.tool.shell.shutil.which", lambda name: name + ".exe")
    probe = AsyncMock(return_value={"status": "success", "data": {"stdout": "linux"}})
    monkeypatch.setattr("reading_agent.agents.tool.shell.run_process", probe)
    assert (await select_backend(workspace, PermissionPolicy()))[0] == "docker"
    probe.return_value = {"status": "error", "data": {}}
    assert (await select_backend(workspace, PermissionPolicy()))[0] == "srt"
    monkeypatch.setattr("reading_agent.agents.tool.shell.shutil.which", lambda _: None)
    monkeypatch.setattr("reading_agent.agents.tool.shell.local_shell", lambda: [sys.executable])
    assert (await select_backend(workspace, PermissionPolicy()))[0] == "host"
    with pytest.raises(OSError):
        await select_backend(workspace, PermissionPolicy(allow_host_shell=False))


async def test_srt_explicit_rules_and_no_silent_downgrade(tmp_path, monkeypatch):
    import json

    workspace = Workspace(tmp_path / "work")

    async def execute(argv, cwd, seconds):
        assert argv[0] == "srt.exe" and argv[1] == "--settings"
        settings = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        assert settings["network"]["allowedDomains"] == []
        assert settings["filesystem"]["allowWrite"] == [str(workspace.root)]
        assert str(os.path.expanduser("~")) in settings["filesystem"]["denyRead"]
        return {"status": "error", "error": "command_failed", "data": {}}

    runner = AsyncMock(side_effect=execute)
    monkeypatch.setattr("reading_agent.agents.tool.shell.run_process", runner)
    monkeypatch.setattr(
        "reading_agent.agents.tool.shell.local_shell",
        lambda: ["bash", "--noprofile", "--norc", "-c"],
    )
    result = await execute_local_backend(workspace, "srt", "srt.exe", "printf test", 2)
    assert result["status"] == "error" and runner.await_count == 1


async def test_real_process_environment_exit_and_output_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "must-not-inherit")
    result = await run_process(
        [sys.executable, "-c", 'import os;print(os.getenv("AGENT_API_KEY","absent"))'], tmp_path, 5
    )
    assert result["status"] == "success" and result["data"]["stdout"].strip() == "absent"
    result = await run_process([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, 5)
    assert result["error"] == "command_failed" and result["data"]["exit_code"] == 7
    result = await run_process([sys.executable, "-c", 'print("x"*50000)'], tmp_path, 5)
    assert result["error"] == "command_output_limit"
    assert len(result["data"]["stdout"]) <= 32768


async def test_real_process_timeout_and_cancel(tmp_path):
    result = await run_process([sys.executable, "-c", "import time;time.sleep(30)"], tmp_path, 0.1)
    assert result["error"] == "command_timeout" and result["data"]["cleanup_confirmed"]
    work = asyncio.create_task(
        run_process([sys.executable, "-c", "import time;time.sleep(30)"], tmp_path, 30)
    )
    await asyncio.sleep(0.3)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work


@pytest.mark.parametrize("parent_waits", [False, True])
async def test_real_background_descendant_is_terminated(tmp_path, parent_waits):
    marker = tmp_path / "must-not-appear"
    child = (
        "import time;from pathlib import Path;time.sleep(1.5);Path("
        + repr(str(marker))
        + ').write_text("leaked")'
    )
    parent = (
        'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'
        + repr(child)
        + "]);"
        + ("time.sleep(30)" if parent_waits else "")
    )
    result = await run_process([sys.executable, "-c", parent], tmp_path, 0.4 if parent_waits else 5)
    assert result["error"] == ("command_timeout" if parent_waits else None)
    await asyncio.sleep(1.6)
    assert not marker.exists()
