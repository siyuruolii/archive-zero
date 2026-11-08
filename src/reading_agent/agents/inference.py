"""无工具的辅助结构化抽取。供历史选择、压缩、记忆候选使用，不自治写库。"""

import asyncio
import json

from reading_agent.agents.provider import ModelRequest, ProviderError


async def infer(provider, schema, instruction, data):
    settings = getattr(provider, "settings", None)
    messages = [
        {
            "role": "system",
            "content": instruction
            + "\n仅输出合法 JSON，遵守此 Schema："
            + json.dumps(schema.model_json_schema(), ensure_ascii=False),
        },
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    output_budget = min(2048, getattr(settings, "output_token_budget", 2048))
    limit = min(100000, getattr(settings, "context_byte_budget", 131072))
    request = ModelRequest(messages, [], schema.model_json_schema(), output_budget)
    size = len(
        json.dumps(
            {"messages": messages, "tools": [], "output_schema": request.output_schema},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    if size + output_budget > limit:
        raise ValueError("辅助推断上下文超限，请缩小本次资料范围")
    async with asyncio.timeout(90):
        for attempt in range(3):
            try:
                final = None
                stream = provider.stream(request)
                try:
                    async for event in stream:
                        if event.kind == "turn":
                            if final is not None:
                                raise ValueError("multiple final turns")
                            final = event.turn
                finally:
                    await stream.aclose()
                if final is None or final.finish_reason != "stop" or final.calls:
                    raise ValueError("incomplete structured inference")
                if len(final.content) > 32000:
                    raise ValueError("inference output too large")
                return schema.model_validate_json(final.content)
            except ProviderError as exc:
                if not exc.retryable or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * 2**attempt)
