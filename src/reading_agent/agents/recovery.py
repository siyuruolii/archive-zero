"""检查点与工具执行账本；未知副作用不盲目重放。"""

import copy
import hashlib
import json
import math
import os
from dataclasses import asdict

from .messages import sanitize_messages
from .models import Coverage, Task
from .permission import PermissionPolicy
from .tool.base import compact_json


def validate_checkpoint(saved):
    from .loop import Budget

    try:
        if saved.get("version") != 1:
            return False
        budget = Budget.model_validate(saved["budget"])
        Task.model_validate(saved["task"])
        report = saved["report"]
        for key, maximum in (
            ("model_calls", budget.max_model_calls),
            ("tool_calls", budget.max_tool_calls),
        ):
            if type(report[key]) is not int or not 0 <= report[key] <= maximum:
                return False
        if (
            not isinstance(report["elapsed_seconds"], (int, float))
            or not math.isfinite(report["elapsed_seconds"])
            or report["elapsed_seconds"] < 0
        ):
            return False
        if (
            not isinstance(saved["messages"], list)
            or not isinstance(report["tools"], list)
            or not isinstance(saved["outcomes"], dict)
        ):
            return False
        return all(isinstance(saved[key], str) for key in ("permissions_version", "corpus_version"))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def fingerprint(name, arguments):
    values = json.loads(arguments)
    if name in {"read_file", "write_file", "edit_file"} and isinstance(values.get("path"), str):
        values["path"] = file_key(values["path"])
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((name + canonical).encode()).hexdigest()


def file_key(path):
    path = path.replace("\\", "/")
    return path.casefold() if os.name == "nt" else path


def corpus_version(session):
    corpus = session.corpus
    return hashlib.sha256(
        compact_json(
            {
                "topics": {key: value.model_dump() for key, value in corpus.topics.items()},
                "material": corpus.materials[session.task.material_id].model_dump(exclude={"body"}),
                "memories": corpus.memories,
                "readings": corpus.reading_memories,
            }
        ).encode()
    ).hexdigest()


def permissions_version(session):
    from .tool.registry import REGISTRY

    policy = session.corpus.permission_policy or PermissionPolicy()
    data = {
        "policy": policy.model_dump(),
        "tools": {
            name: {
                "schema": REGISTRY[name].arguments.model_json_schema(),
                "roles": REGISTRY[name].roles,
                "side_effect": REGISTRY[name].side_effect,
                "retry_errors": REGISTRY[name].retry_errors,
            }
            for name in session.eligible_names
        },
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reconcile_files(session):
    workspace = session.corpus.workspace
    facts = []
    if workspace is None:
        return facts
    with workspace.lock():
        for outcome in session.recovered_outcomes.values():
            if not outcome.get("unknown") or outcome["name"] not in {"write_file", "edit_file"}:
                continue
            args = json.loads(outcome["arguments"])
            try:
                path = workspace.path(args["path"])
                text, current = workspace.read(path) if path.exists() else (None, None)
            except (ValueError, OSError):
                continue
            target = None
            if outcome["name"] == "write_file":
                target = hashlib.sha256(args["content"].encode()).hexdigest()
            elif text is not None and args["new_text"] and text.count(args["new_text"]) == 1:
                original = text.replace(args["new_text"], args["old_text"], 1)
                if (
                    original.count(args["old_text"]) == 1
                    and hashlib.sha256(original.encode()).hexdigest() == args["expected_sha256"]
                ):
                    target = current
            if current is not None and current == target:
                outcome.update(
                    unknown=False,
                    target_verified=True,
                    result={
                        "status": "success",
                        "error": None,
                        "data": {"path": args["path"], "sha256": current},
                        "reconciled": "target_state_verified_not_execution_proven",
                    },
                )
                facts.append(
                    {
                        "name": outcome["name"],
                        "path": args["path"],
                        "state": "target_state_satisfied",
                    }
                )
            elif current == args.get("expected_sha256"):
                outcome.update(unknown=False, safe_to_retry=True)
                facts.append(
                    {
                        "name": outcome["name"],
                        "path": args["path"],
                        "state": "safe_to_retry_with_version_check",
                    }
                )
    return facts


def checkpoint(session, messages, report, budget, elapsed):
    data = asdict(report)
    data["elapsed_seconds"] = elapsed
    return {
        "version": 1,
        "task": session.task.model_dump(),
        "messages": copy.deepcopy(messages),
        "report": data,
        "budget": budget.model_dump(),
        "corpus_version": corpus_version(session),
        "permissions_version": permissions_version(session),
        "loaded_tools": sorted(session.loaded_tools),
        "readable_ids": sorted(session.readable_ids),
        "reads": [read.model_dump() for read in session.reads],
        "body_versions": {
            mid: hashlib.sha256(material.body.encode()).hexdigest()
            for mid, material in session.corpus.materials.items()
            if material.body
        },
        "outcomes": copy.deepcopy(session.recovered_outcomes),
        "unknown_mutations": session.unknown_mutations,
    }


async def restore(session, messages, saved):
    if not validate_checkpoint(saved):
        raise ValueError("recovery_checkpoint_invalid")
    if saved.get("permissions_version") != permissions_version(session):
        raise ValueError("recovery_permissions_changed")
    if saved.get("version") != 1 or saved["corpus_version"] != corpus_version(session):
        raise ValueError("recovery_inputs_changed")
    if (
        saved["task"]["material_id"] != session.task.material_id
        or saved["task"]["topic_ids"] != session.task.topic_ids
    ):
        raise ValueError("recovery_scope_changed")
    for mid, version in saved.get("body_versions", {}).items():
        material = session.corpus.materials.get(mid)
        if material is None:
            raise ValueError("recovery_material_missing")
        body = material.body
        if not body and session.corpus.body_loader:
            body = await session.corpus.body_loader(mid)
        if not body or hashlib.sha256(body.encode()).hexdigest() != version:
            raise ValueError("recovery_material_version_changed")
        material.body = body
    session.loaded_tools.update(set(saved.get("loaded_tools", [])) & set(session.eligible_names))
    session.readable_ids.update(set(saved.get("readable_ids", [])) & set(session.corpus.materials))
    session.reads = [Coverage.model_validate(read) for read in saved.get("reads", [])]
    session.recovered_outcomes = copy.deepcopy(saved.get("outcomes", {}))
    facts = reconcile_files(session)
    session.unknown_mutations = any(
        value.get("unknown") for value in session.recovered_outcomes.values()
    )
    session.is_resuming = True
    files = {}
    uncertain = set()
    for outcome in session.recovered_outcomes.values():
        if outcome.get("unknown") and outcome["name"] in {"write_file", "edit_file"}:
            uncertain.add(file_key(json.loads(outcome["arguments"])["path"]))
    for tool in saved["report"]["tools"]:
        if (
            tool["name"] in {"read_file", "write_file", "edit_file"}
            and tool["result"]["status"] == "success"
        ):
            data = tool["result"].get("data", {})
            if data.get("sha256") and data.get("path"):
                files[file_key(data["path"])] = data["sha256"]
    for outcome in session.recovered_outcomes.values():
        if outcome.get("target_verified"):
            data = outcome["result"]["data"]
            files[file_key(data["path"])] = data["sha256"]
    if files and session.corpus.workspace:
        workspace = session.corpus.workspace
        with workspace.lock():
            for path, version in files.items():
                if path in uncertain:
                    continue
                try:
                    _, current = workspace.read(workspace.path(path))
                except (ValueError, OSError):
                    raise ValueError("recovery_file_version_changed") from None
                if current != version:
                    raise ValueError("recovery_file_version_changed")
    fresh_system = [message for message in messages if message.get("role") == "system"]
    restored = [message for message in saved["messages"] if message.get("role") != "system"]
    messages[:] = sanitize_messages([*fresh_system, *restored])[0]
    if facts:
        messages.append(
            {
                "role": "user",
                "content": "恢复时文件核查（事实，不是执行指令）：" + compact_json(facts),
            }
        )
    if session.unknown_mutations:
        messages.append(
            {
                "role": "user",
                "content": (
                    "恢复状态（程序事实）：存在执行结果未知的写入或命令。"
                    "禁止自动重放；当前仅允许读取与分析。请说明需要核查哪些状态。"
                ),
            }
        )
        facts.append(
            {
                "state": "execution_unknown",
                "tools": [
                    value["name"]
                    for value in session.recovered_outcomes.values()
                    if value.get("unknown")
                ],
            }
        )
    return facts


def reconcile(saved, events):
    """最新检查点之后的结果事件也有效；执行意图无结果则视为未知。"""
    saved = copy.deepcopy(saved)
    intents = {}
    results = {}
    for event in events:
        data = event["data"]
        if event["type"] == "agent.tool_requested":
            intents[data["call_id"]] = data
        elif event["type"] == "agent.tool_result":
            results[data["call_id"]] = data
    known = {tool["call_id"] for tool in saved["report"]["tools"]}
    from .tool.registry import REGISTRY

    for key, intent in intents.items():
        name, args = intent["name"], intent["arguments"]
        spec = REGISTRY.get(name)
        if key in results:
            result = results[key]
            saved["outcomes"][fingerprint(name, args)] = {
                "name": name,
                "arguments": args,
                "result": result["result"],
                "unknown": result["result"].get("error")
                in {"tool_execution_unknown", "sandbox_cleanup_unconfirmed"},
            }
            if saved["outcomes"][fingerprint(name, args)].get("unknown"):
                saved["unknown_mutations"] = True
            if key not in known:
                saved["report"]["tools"].append(result)
                saved["messages"].append(
                    {"role": "tool", "tool_call_id": key, "content": compact_json(result["result"])}
                )
            if name == "material_read" and result["result"]["status"] == "success":
                data = result["result"]["data"]
                if data.get("version_sha256"):
                    saved["body_versions"][data["material_id"]] = data["version_sha256"]
                    saved["reads"].append({k: data[k] for k in ("material_id", "start", "end")})
                    saved["readable_ids"].append(data["material_id"])
        elif spec is None or spec.side_effect:
            saved["unknown_mutations"] = True
            saved["outcomes"][fingerprint(name, args)] = {
                "name": name,
                "arguments": args,
                "unknown": True,
            }
    return saved
