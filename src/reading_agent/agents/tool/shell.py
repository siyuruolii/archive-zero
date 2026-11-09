"""命令后端按 Docker / srt / 本机选择；每次确认绑定实际后端。"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from reading_agent.domain.models import StrictModel
from reading_agent.infrastructure.processes import run_process
from reading_agent.storage.database import Conflict

from ..permission import PermissionPolicy, check_permission
from .base import ToolSpec, error
from .filesystem import available


class BashArgs(StrictModel):
    command: str = Field(min_length=1, max_length=8000)
    timeout_seconds: int = Field(ge=1, le=60)


class OutputLimit(Exception):
    pass


def container_arguments(workspace, name, command):
    if not workspace.image or not re.fullmatch(r"sha256:[a-f0-9]{64}", workspace.image):
        raise ValueError("sandbox_image_not_configured")
    # 不使用宿主 bind mount；输入来自命令文本，容器看不到任何宿主路径。
    return [
        "run",
        "--name",
        name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=64",
        "--memory=256m",
        "--memory-swap=256m",
        "--cpus=1",
        "--user=65534:65534",
        "--log-driver=none",
        "--ulimit",
        "fsize=1048576:1048576",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--workdir=/tmp",
        "--entrypoint=/bin/bash",
        workspace.image,
        "--noprofile",
        "--norc",
        "-c",
        command,
    ]


async def execute_container(workspace, command, seconds, *, spawn=asyncio.create_subprocess_exec):
    executable = shutil.which("docker")
    if not executable:
        return error("sandbox_runtime_unavailable")
    name = "archive-zero-" + uuid4().hex
    try:
        argv = container_arguments(workspace, name, command)
    except ValueError as exc:
        return error(str(exc))
    # 崩溃或清理失败的容器名称留作人工恢复依据，不携带命令或秘密。
    lease = workspace.root / (".sandbox-" + name + ".json")
    try:
        with workspace.lock():
            if any(workspace.root.glob(".sandbox-*.json")):
                return error("sandbox_recovery_required")
            with lease.open("x", encoding="utf-8") as stream:
                json.dump({"container": name, "image": workspace.image}, stream)
    except Conflict:
        return error("workspace_busy")
    except OSError:
        return error("sandbox_lease_failed")
    # Docker CLI 也不继承 AGENT_*、云凭据、代理和模型密钥。
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOME"}
    }
    options = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    process = None
    creation = asyncio.create_task(
        spawn(executable, *argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    )
    pumps = []
    buffers = [bytearray(), bytearray()]
    total = 0
    outcome = None

    async def collect(stream, buffer):
        nonlocal total
        while chunk := await stream.read(4096):
            room = max(0, 32768 - total)
            buffer.extend(chunk[:room])
            total += len(chunk)
            if total > 32768:
                raise OutputLimit

    async def cleanup():
        try:
            removal = await spawn(
                executable,
                "rm",
                "-f",
                "-v",
                name,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **options,
            )
        except OSError:
            return False
        try:
            await asyncio.wait_for(removal.wait(), 5)
            return removal.returncode == 0
        except TimeoutError:
            removal.kill()
            await removal.wait()
            return False

    try:
        process = await asyncio.shield(creation)
        pumps = [
            asyncio.create_task(collect(process.stdout, buffers[0])),
            asyncio.create_task(collect(process.stderr, buffers[1])),
            asyncio.create_task(process.wait()),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*pumps), seconds)
            outcome = "success" if process.returncode == 0 else "command_failed"
        except TimeoutError:
            outcome = "command_timeout"
        except OutputLimit:
            outcome = "command_output_limit"
    except OSError:
        return error("sandbox_start_failed")
    finally:
        if process is None:
            try:
                process = await asyncio.shield(creation)
            except OSError:
                pass
        clean = False
        if process is not None:
            removal_task = asyncio.create_task(cleanup())
            try:
                clean = await asyncio.shield(removal_task)
            except asyncio.CancelledError:
                clean = await removal_task
                raise
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                for pump in pumps:
                    if not pump.done():
                        pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        if clean or process is None:
            lease.unlink(missing_ok=True)
    if not clean:
        return error("sandbox_cleanup_unconfirmed")
    return {
        "status": "success" if outcome == "success" else "error",
        "error": None if outcome == "success" else outcome,
        "data": {
            "stdout": buffers[0].decode("utf-8", errors="replace"),
            "stderr": buffers[1].decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "cleanup_confirmed": clean,
        },
    }


def local_shell():
    if os.name == "nt":
        executable = (
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if executable.is_file():
            return [str(executable), "-NoProfile", "-NonInteractive", "-Command"]
    else:
        executable = shutil.which("bash")
        if executable:
            return [executable, "--noprofile", "--norc", "-c"]
    raise OSError("shell_unavailable")


async def select_backend(workspace, policy):
    docker = shutil.which("docker")
    if policy.allow_container and docker and workspace.image:
        probe = await run_process([docker, "info", "--format", "{{.OSType}}"], workspace.root, 3)
        if probe["status"] == "success" and "linux" in probe["data"]["stdout"].casefold():
            return "docker", docker
    srt = shutil.which("srt")
    if policy.allow_srt and srt:
        if os.name != "nt" or srt.lower().endswith(".exe"):
            return "srt", srt
        # npm Windows包装器不经cmd执行：定位同目录受信包的Node CLI入口。
        cli = Path(srt).parent / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
        if cli.is_file() and shutil.which("node"):
            return "srt", str(cli)
    if policy.allow_host_shell:
        return "host", local_shell()[0]
    raise OSError("no_permitted_command_backend")


async def execute_local_backend(workspace, backend, executable, command, seconds):
    argv = [*local_shell(), command]
    if backend == "host":
        return await run_process(argv, workspace.root, seconds)
    home = Path.home()
    protected = [str(home / name) for name in (".ssh", ".aws", ".azure", ".codex", ".config")]
    settings = {
        "network": {"allowedDomains": [], "deniedDomains": [], "allowLocalBinding": False},
        "filesystem": {
            "denyRead": [str(home), *protected],
            "allowRead": [str(workspace.root)],
            "allowWrite": [str(workspace.root)],
            "denyWrite": [str(workspace.root / ".workspace.lock")],
        },
    }
    with tempfile.TemporaryDirectory(prefix="archive-zero-srt-") as folder:
        config = Path(folder) / "settings.json"
        config.write_text(json.dumps(settings), encoding="utf-8")
        launcher = (
            [shutil.which("node"), executable] if executable.endswith(".js") else [executable]
        )
        return await run_process(
            [*launcher, "--settings", str(config), "--", *argv], workspace.root, seconds
        )


async def bash(session, args):
    policy = session.corpus.permission_policy or PermissionPolicy()
    approval = session.corpus.command_approval
    if approval is None:
        return error("user_confirmation_required")
    try:
        backend, executable = await select_backend(session.corpus.workspace, policy)
    except OSError:
        return error("no_permitted_command_backend")
    labels = {
        "docker": "Docker 禁网临时容器，无宿主挂载",
        "srt": "srt 系统沙箱，限制网络与文件写入",
        "host": "本机 Shell：无 OS 隔离，可访问当前用户资源，仅清理继承环境和限制运行时间",
    }
    environment = labels[backend] + " · " + executable
    if not await approval(args.command, args.timeout_seconds, environment):
        return error("user_confirmation_denied_or_expired")
    denied = check_permission("bash", args, session.corpus.permission_policy)
    if denied:
        return error(denied)
    try:
        if backend == "docker":
            return await execute_container(
                session.corpus.workspace, args.command, args.timeout_seconds
            )
        return await execute_local_backend(
            session.corpus.workspace, backend, executable, args.command, args.timeout_seconds
        )
    except OSError:
        return error("command_backend_failed")


TOOLS = (
    ToolSpec(
        "bash",
        "运行命令，优先Docker，再srt，最后本机Shell；须逐次确认，限时60秒、输出32KiB。",
        BashArgs,
        ("discuss",),
        guideline=(
            "每次执行须由用户在界面确认，发现工具不代表批准。用于临时计算与验证；"
            "Windows本机回退使用PowerShell语法，POSIX使用Bash。"
            "确认界面显示实际后端。Docker不挂载宿主文件；srt和本机在专用工作区启动。"
            "产物从 stdout 返回，再用 write_file 保存。"
            "本机回退无OS隔离。后端执行失败不会在同一确认下偷偷降级。"
        ),
        arun=bash,
        available=available,
        cacheable=False,
    ),
)
