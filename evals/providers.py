"""受控离线响应与跨模型/阶段共享的真实调用上限。"""

import asyncio
import json
from contextlib import aclosing

from reading_agent.agents.provider import ProviderError, ProviderEvent, ToolCall, Turn


class CallLimit:
    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0


class CappedProvider:
    def __init__(self, provider, limit):
        self.provider, self.limit = provider, limit

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def stream(self, request):
        if self.limit.used >= self.limit.maximum:
            raise ProviderError("eval_call_budget_exhausted")
        self.limit.used += 1
        async with aclosing(self.provider.stream(request)) as stream:
            async for event in stream:
                yield event


class ScriptedProvider:
    model_name = "eval-scripted-contract-only"

    def __init__(self, steps):
        self.steps = list(steps)

    async def stream(self, request):
        if not self.steps:
            raise ProviderError("eval_script_exhausted")
        step = self.steps.pop(0)
        if "tool" in step:
            yield ProviderEvent(
                "turn",
                turn=Turn(
                    calls=[
                        ToolCall(
                            "eval-" + str(len(self.steps)),
                            step["tool"],
                            json.dumps(step["arguments"], ensure_ascii=False),
                        )
                    ],
                    finish_reason="tool_calls",
                ),
            )
            return
        content = step.get("raw") or json.dumps(step["final"], ensure_ascii=False)
        for position in range(0, len(content), 24):
            yield ProviderEvent("text", text=content[position : position + 24])
        if step.get("hang"):
            await asyncio.Event().wait()
        yield ProviderEvent("turn", turn=Turn(content=content))
