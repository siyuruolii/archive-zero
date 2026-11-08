"""可展示的数值快照；不返回提示词、工具参数、正文或凭据。"""

import json


def byte_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def context_metrics(request, limit, *, model=None, window=None):
    sizes = {"system": 0, "conversation": 0, "tool_messages": 0}
    for message in request.messages:
        kind = (
            "system"
            if message["role"] == "system"
            else (
                "tool_messages"
                if message["role"] == "tool" or message.get("tool_calls")
                else "conversation"
            )
        )
        sizes[kind] += byte_size(message)
    sizes["tool_definitions"] = byte_size(request.tools)
    sizes["output_schema"] = byte_size(request.output_schema)
    total = byte_size(
        {
            "messages": request.messages,
            "tools": request.tools,
            "output_schema": request.output_schema,
        }
    )
    sizes["framing"] = total - sum(sizes.values())
    return {
        "model": model,
        "window_tokens": window,
        "input_tokens": None,
        "input_bytes": total,
        "budget_bytes": limit,
        "output_reserve_tokens": request.max_output_tokens,
        "budget_used": total + request.max_output_tokens,
        "breakdown_bytes": sizes,
        "basis": "utf8_conservative_proxy",
    }


def run_metrics(report, *, elapsed=None):
    hit = miss = covered = 0
    received = 0
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    coverage = {key: 0 for key in totals}
    calls = []
    for attempt in report.attempts:
        usage = attempt.get("usage", {})
        received += attempt.get("status") == "received"
        h, m = attempt.get("cache_hit_tokens"), attempt.get("cache_miss_tokens")
        if type(h) is int and type(m) is int and h >= 0 and m >= 0:
            hit += h
            miss += m
            covered += 1
        for key in totals:
            if type(usage.get(key)) is int and usage[key] >= 0:
                totals[key] += usage[key]
                coverage[key] += 1
        calls.append(
            {
                key: attempt.get(key)
                for key in (
                    "call",
                    "purpose",
                    "model",
                    "status",
                    "elapsed_seconds",
                    "cache_hit_tokens",
                    "cache_miss_tokens",
                )
            }
            | {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
            }
        )
    return {
        "version": 1,
        "scope": "agent_loop",
        "model": report.model,
        "phase": "running" if report.end_reason == "not_started" else report.status,
        "end_reason": report.end_reason,
        "elapsed_seconds": report.elapsed_seconds if elapsed is None else round(elapsed, 3),
        "model_calls": report.model_calls,
        "tool_calls": report.tool_calls,
        "compactions": sum(bool(e.get("changed")) for e in report.compaction_events),
        "tokens": {k: totals[k] if coverage[k] else None for k in totals},
        "usage_calls": coverage,
        "received_calls": received,
        "usage_complete": report.usage_complete,
        "cache": {
            "hit_tokens": hit if covered else None,
            "miss_tokens": miss if covered else None,
            "hit_rate": hit / (hit + miss) if hit + miss else None,
            "reported_calls": covered,
            "attempted_calls": report.model_calls,
        },
        "context": report.observability.get("context"),
        "calls": calls,
    }
