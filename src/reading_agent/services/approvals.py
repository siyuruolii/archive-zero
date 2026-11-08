"""人类确认控制面；不注册为模型工具，不持久化或复用同意。"""

import asyncio

from reading_agent.storage.database import Conflict, NotFound, identity


class CommandApprovals:
    def __init__(self, notify, seconds=60):
        self.notify, self.seconds = notify, seconds
        self.pending = {}

    async def request(self, scope, command, seconds, environment="禁网临时容器，无宿主文件挂载"):
        if len(self.pending) >= 16:
            return False
        key = identity()
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = (
            {
                "id": key,
                "scope": scope,
                "command": command,
                "timeout_seconds": seconds,
                "environment": environment,
            },
            future,
        )
        self.notify()
        try:
            return await asyncio.wait_for(future, self.seconds)
        except TimeoutError:
            return False
        finally:
            self.pending.pop(key, None)
            self.notify()

    def list(self):
        return [dict(record) for record, future in self.pending.values() if not future.done()]

    def resolve(self, key, approved):
        if key not in self.pending:
            raise NotFound("approval expired")
        _, future = self.pending[key]
        if future.done():
            raise Conflict("approval already resolved")
        future.set_result(approved)
        self.notify()
