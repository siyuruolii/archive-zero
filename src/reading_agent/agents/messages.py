"""模型消息副本清洗；原始事件保留，失败结果也是有效配对。"""

import copy
from collections import Counter


def sanitize_messages(messages):
    removed = sum(not isinstance(message, dict) for message in messages)
    messages = [copy.deepcopy(message) for message in messages if isinstance(message, dict)]
    ids = Counter(
        call.get("id")
        for message in messages
        if message.get("role") == "assistant"
        for call in (
            message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        )
        if isinstance(call, dict) and isinstance(call.get("id"), str)
    )
    cleaned = []
    position = 0
    while position < len(messages):
        message = messages[position]
        position += 1
        if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            removed += 1
            continue
        if message.get("role") == "tool":
            removed += 1
            continue
        calls = message.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            removed += 1
            message.pop("tool_calls", None)
            calls = None
        if message.get("role") != "assistant" or not calls:
            message.pop("tool_calls", None)
            if message.get("role") != "assistant" or message.get("content"):
                cleaned.append(message)
            continue
        results = []
        while position < len(messages) and messages[position].get("role") == "tool":
            results.append(messages[position])
            position += 1
        counts = Counter(
            result.get("tool_call_id")
            for result in results
            if isinstance(result.get("tool_call_id"), str)
        )
        valid = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            key = call.get("id")
            function = call.get("function", {})
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 256
                or ids[key] != 1
                or counts[key] != 1
                or call.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function.get("name")
            ):
                continue
            if isinstance(function.get("arguments"), str):
                valid.append(call)
        keys = {call["id"] for call in valid}
        paired = [
            result
            for result in results
            if isinstance(result.get("tool_call_id"), str)
            and result.get("tool_call_id") in keys
            and isinstance(result.get("content"), str)
        ]
        keys = {result["tool_call_id"] for result in paired}
        valid = [call for call in valid if call["id"] in keys]
        removed += len(calls) - len(valid) + len(results) - len(paired)
        if valid:
            message["tool_calls"] = valid
            cleaned.extend([message, *paired])
        elif message.get("content"):
            message.pop("tool_calls", None)
            cleaned.append(message)
    return cleaned, removed
