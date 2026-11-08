"""命令进程树生命周期：Windows Job kill-on-close / POSIX 独立进程组。"""

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys


class WindowsJob:
    def __init__(self, pid):
        from ctypes import wintypes as wt

        class Basic(ctypes.Structure):
            _fields_ = [
                ("time1", ctypes.c_longlong),
                ("time2", ctypes.c_longlong),
                ("flags", wt.DWORD),
                ("min", ctypes.c_size_t),
                ("max", ctypes.c_size_t),
                ("count", wt.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wt.DWORD),
                ("class_", wt.DWORD),
            ]

        class Info(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.restype = wt.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wt.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        self.api.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.api.OpenProcess.restype = wt.HANDLE
        self.api.CloseHandle.argtypes = [wt.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        info = Info()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if (
                not self.handle
                or not process
                or not self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
                )
                or not self.api.AssignProcessToJobObject(self.handle, process)
            ):
                self.close()
                raise OSError("process_tree_isolation_failed")
        finally:
            if process:
                self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def run_process(argv, cwd, seconds):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "PATHEXT",
        }
    }
    # 不继承用户/项目环境中的密钥与代理；Shell 不加载启动配置。
    worker = (
        [sys.executable, "--shell-worker"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "reading_agent.infrastructure.shell_worker"]
    )
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *worker,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await creation
        process.kill()  # 启动器尚未收到命令，没有不受管的后代。
        await process.wait()
        raise
    job = None
    tasks = []
    buffers = [bytearray(), bytearray()]
    total = 0
    reason = None
    tree_stopped = False
    exit_observed = asyncio.Event()
    timer = None

    def observe_exit():
        nonlocal timer
        if process.returncode is None:
            timer = asyncio.get_running_loop().call_later(0.02, observe_exit)
        else:
            exit_observed.set()

    def stop_tree():
        nonlocal tree_stopped
        if tree_stopped:
            return
        tree_stopped = True
        if job:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def wait_exit():
        # wait() 可能等待后代持有的管道EOF；先看退出码，及时清理后台后代。
        await exit_observed.wait()
        stop_tree()
        return await process.wait()

    async def collect(stream, target):
        nonlocal total
        while chunk := await stream.read(4096):
            target.extend(chunk[: max(0, 32768 - total)])
            total += len(chunk)
            if total > 32768:
                raise BufferError("command_output_limit")

    try:
        if os.name == "nt":
            job = WindowsJob(process.pid)
        process.stdin.write(json.dumps({"argv": argv}).encode() + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        observe_exit()
        tasks = [
            asyncio.create_task(collect(process.stdout, buffers[0])),
            asyncio.create_task(collect(process.stderr, buffers[1])),
            asyncio.create_task(wait_exit()),
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), seconds)
            reason = None if process.returncode == 0 else "command_failed"
        except TimeoutError:
            reason = "command_timeout"
        except BufferError:
            reason = "command_output_limit"
    finally:
        # 即使父进程已正常退出，仍终止它留下的后台子进程。
        stop_tree()
        if timer is not None:
            timer.cancel()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "status": "error" if reason else "success",
        "error": reason,
        "data": {
            "stdout": buffers[0].decode("utf-8", errors="replace"),
            "stderr": buffers[1].decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "cleanup_confirmed": True,
        },
    }
