"""离线优先、独立工作区、受预算限制的 Harness eval 入口。"""

import argparse
import asyncio
import hashlib
import itertools
import json
import platform
import statistics
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from reading_agent.agents.harness import Harness
from reading_agent.agents.model_router import ModelRouter
from reading_agent.agents.permission import PermissionPolicy
from reading_agent.agents.prompts import PROMPT_VERSION, RuntimeEnvironment
from reading_agent.agents.tool import Corpus
from reading_agent.domain.models import Material, Topic
from reading_agent.infrastructure.configuration import load_catalog
from reading_agent.infrastructure.workspace import Workspace
from reading_agent.services.memory import Memory
from reading_agent.services.memory_consolidation import ConsolidationPolicy
from reading_agent.storage.database import Store

from .graders import RUBRIC, grade
from .providers import CallLimit, CappedProvider, ScriptedProvider
from .schema import Dataset

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def execute_case(case, provider, folder, topics, as_of):
    corpus = Corpus(
        topics={t["id"]: Topic.model_validate(t) for t in topics},
        materials={
            "article": Material(
                id="article",
                title=case.title,
                source_summary=case.summary,
                body=case.body,
                url="fixture://article",
            )
        },
    )
    workspace = Workspace(folder / "workspace")
    if case.workspace:
        corpus.workspace = workspace
    corpus.permission_policy = PermissionPolicy(
        allow_container=False, allow_srt=False, allow_host_shell=False
    )
    memory = None
    if case.memories:
        store = Store(folder / "reading.db")
        with store.transaction() as db:
            for topic in topics:
                db.execute(
                    "INSERT INTO topics VALUES(?,?,?,?)",
                    (topic["id"], topic["name"], topic["description"], as_of),
                )
        memory = Memory(store, None, consolidation_policy=ConsolidationPolicy(enabled=False))
        count = itertools.count()
        with (
            patch("reading_agent.services.memory.service.now", lambda: as_of),
            patch(
                "reading_agent.services.memory.service.identity",
                lambda: digest((case.id + str(next(count))).encode())[:32],
            ),
        ):
            for seed in case.memories:
                mid = memory.add(
                    seed["content"],
                    topic_id=seed.get("topic_id"),
                    kind=seed.get("kind", "profile"),
                    details=seed.get("details"),
                )
                if seed.get("corrected"):
                    memory.correct(mid, seed["corrected"])
                if seed.get("deleted"):
                    memory.delete(mid)
    events = []
    started = time.monotonic()
    first_response = None
    first_visible = None
    signal = asyncio.Event()

    def observe(event):
        nonlocal first_response, first_visible
        events.append(event.kind)
        if event.kind == "text_delta" and first_response is None:
            first_response = round(time.monotonic() - started, 6)
        if event.kind in {"answer_delta", "deep_summary_delta"} and first_visible is None:
            first_visible = round(time.monotonic() - started, 6)
        if case.cancel_on_text and event.kind == "text_delta":
            signal.set()

    try:
        report = await Harness(
            provider,
            corpus,
            case.budget,
            memory=memory,
            runtime=RuntimeEnvironment(
                captured_at=datetime.fromisoformat(as_of),
                operating_system="eval-fixture",
                python_version="fixed",
            ),
        ).run(case.task, cancel=signal, on_event=observe)
        result = grade(case, report, workspace)
        metrics = {
            "model": report.model,
            "status": report.status,
            "end_reason": report.end_reason,
            "elapsed_seconds": report.elapsed_seconds,
            "first_response_seconds": first_response,
            "first_visible_text_seconds": first_visible,
            "model_calls": report.model_calls,
            "tool_calls": report.tool_calls,
            "usage": report.usage if report.usage else None,
            "usage_complete": report.usage_complete,
            "context_usage": report.context_usage,
            "compactions": len(report.compaction_events),
            "observability": report.observability,
        }
        return {
            **result,
            "result": report.result,
            "metrics": metrics,
            "event_types": events,
            "actual_readings": report.readings,
            "tool_outcomes": [
                {
                    "name": tool["name"],
                    "status": tool["result"]["status"],
                    "error": tool["result"].get("error"),
                }
                for tool in report.tools
            ],
        }
    finally:
        if memory:
            await memory.shutdown()


def public_config(router):
    if router is None:
        return {"model": "eval-scripted-contract-only"}
    return {
        "default": router.catalog.default,
        "stages": router.catalog.stages,
        "profiles": {
            name: settings.model_dump(
                include={
                    "model",
                    "adapter",
                    "json_mode",
                    "supports_tools",
                    "strict_tools",
                    "cache_mode",
                    "context_byte_budget",
                    "context_window_tokens",
                    "output_token_budget",
                    "reasoning_effort",
                }
            )
            for name, settings in router.catalog.profiles.items()
        },
    }


async def run(
    dataset_path,
    output,
    *,
    live=False,
    repeat=1,
    maximum_calls=100,
    ids=(),
    profile=None,
    fixtures=None,
):
    if not 1 <= repeat <= 10 or not 1 <= maximum_calls <= 10000:
        raise ValueError("invalid repeat or call limit")
    dataset_path = Path(dataset_path).resolve()
    dataset = Dataset.model_validate_json(dataset_path.read_text(encoding="utf-8"))
    fixtures = Path(fixtures).resolve() if fixtures else ROOT / "evals/fixtures"
    fixture_path = fixtures / "topics.json"
    oracle_path = fixtures / "scripts.json"
    topics = json.loads(fixture_path.read_text(encoding="utf-8"))
    scripts = json.loads(oracle_path.read_text(encoding="utf-8"))
    cases = [case for case in dataset.cases if not ids or case.id in ids]
    if not cases or set(ids) - {case.id for case in dataset.cases}:
        raise ValueError("unknown or empty case selection")
    if any(
        case.id not in scripts for case in cases if not live or case.execution == "fault_contract"
    ):
        raise ValueError("missing scripted oracle")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = stamp + "-" + uuid4().hex[:8]
    output = Path(output).resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    (inputs / "dataset.json").write_bytes(dataset_path.read_bytes())
    (inputs / "topics.json").write_bytes(fixture_path.read_bytes())
    (inputs / "scripts.json").write_bytes(oracle_path.read_bytes())
    total = CallLimit(maximum_calls)
    rows = []
    runtime = datetime.fromisoformat(dataset.as_of)
    if runtime.tzinfo is None:
        raise ValueError("as_of must have a timezone")
    async with httpx.AsyncClient(trust_env=False) as client:
        router = None
        if live:
            catalog = load_catalog()
            if profile:
                if profile not in catalog.profiles:
                    raise ValueError("unknown model profile")
                catalog = catalog.model_copy(update={"default": profile, "stages": {}})
            router = ModelRouter(catalog, client)
            router.providers = {
                name: CappedProvider(provider, total) for name, provider in router.providers.items()
            }
        elif profile:
            raise ValueError("--profile requires --live")
        source_files = sorted((ROOT / "src/reading_agent").rglob("*.py")) + sorted(
            (ROOT / "evals").rglob("*.py")
        )
        source_hash = digest(
            b"".join(str(p.relative_to(ROOT)).encode() + p.read_bytes() for p in source_files)
        )
        manifest = {
            "version": 1,
            "run_id": run_id,
            "mode": "live" if live else "scripted_contract_only",
            "as_of": dataset.as_of,
            "reference_status": dataset.reference_status,
            "dataset_sha256": digest(dataset_path.read_bytes()),
            "fixtures_sha256": digest(fixture_path.read_bytes() + oracle_path.read_bytes()),
            "source_sha256": source_hash,
            "prompt_version": PROMPT_VERSION,
            "python": platform.python_version(),
            "models": public_config(router),
            "maximum_real_calls": maximum_calls,
            "repeat": repeat,
            "selected_ids": [case.id for case in cases],
        }
        dump(output / "manifest.json", manifest)
        with tempfile.TemporaryDirectory(prefix="archive-zero-eval-") as directory:
            for case in cases:
                for attempt in range(1, repeat + 1):
                    mode = (
                        "live" if live and case.execution == "agent" else "scripted_contract_only"
                    )
                    row = {
                        "id": case.id,
                        "category": case.category,
                        "repeat": attempt,
                        "mode": mode,
                        "run_id": run_id,
                    }
                    if mode == "live" and total.used >= maximum_calls:
                        row.update(status="skipped", reason="global_call_budget_exhausted")
                    else:
                        folder = Path(directory) / f"{case.id}-{attempt}"
                        folder.mkdir()
                        provider = router if mode == "live" else ScriptedProvider(scripts[case.id])
                        before = total.used
                        try:
                            row.update(
                                await execute_case(case, provider, folder, topics, dataset.as_of)
                            )
                            row["status"] = "passed" if row["hard_pass"] else "failed"
                        except Exception as exc:
                            # 供应商安全错误代码之外不回显异常正文，避免路径/请求/密钥泄漏。
                            row.update(status="error", reason="runtime:" + type(exc).__name__)
                            row["quality"] = {
                                "status": "pending_human_review"
                                if case.execution == "agent"
                                else "not_applicable",
                                "rubric": RUBRIC,
                            }
                            row["error_frames"] = [
                                {
                                    "file": Path(frame.filename).name,
                                    "line": frame.lineno,
                                    "function": frame.name,
                                }
                                for frame in traceback.extract_tb(exc.__traceback__)
                            ]
                        row["real_calls"] = total.used - before
                        if (
                            mode == "scripted_contract_only"
                            and row.get("quality", {}).get("status") == "pending_human_review"
                        ):
                            row["quality"]["status"] = "not_measured_scripted_output"
                    row["output_sha256"] = digest(
                        json.dumps(row.get("result"), ensure_ascii=False, sort_keys=True).encode()
                    )
                    dump(output / f"{case.id}-{attempt}.json", row)
                    with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows.append(row)
        counts = dict(Counter(row["status"] for row in rows))
        categories = {
            category: dict(Counter(row["status"] for row in rows if row["category"] == category))
            for category in sorted({row["category"] for row in rows})
        }
        summary = {
            "run_id": run_id,
            "mode": manifest["mode"],
            "counts": counts,
            "categories": categories,
            "real_calls": total.used,
            "quality_acceptance": "not_evaluated_pending_human_review",
            "note": "Hard checks and scripted replay do not establish semantic quality.",
        }
        elapsed = sorted(row["metrics"]["elapsed_seconds"] for row in rows if "metrics" in row)
        summary["performance"] = {
            "measured_attempts": len(elapsed),
            "p50_seconds": statistics.median(elapsed) if elapsed else None,
            "p95_seconds": elapsed[max(0, (len(elapsed) * 95 + 99) // 100 - 1)]
            if elapsed
            else None,
            "cost": None,
            "cost_status": "not_configured",
        }
        summary["per_case_successes"] = {
            case.id: {
                "passed": sum(r["status"] == "passed" for r in rows if r["id"] == case.id),
                "attempts": sum(r["id"] == case.id for r in rows),
            }
            for case in cases
        }
        dump(output / "summary.json", summary)
        reviews = [
            {
                "run_id": run_id,
                "id": row["id"],
                "repeat": row["repeat"],
                "output_sha256": row["output_sha256"],
                "scores": {dimension: None for dimension in RUBRIC},
                "evidence": "",
                "reviewer": "",
            }
            for row in rows
            if row.get("quality", {}).get("status") == "pending_human_review"
            and row["mode"] == "live"
        ]
        dump(output / "reviews-template.json", reviews)
        table = [
            "# Agent eval 报告",
            "",
            f"运行：{run_id}",
            f"模式：{manifest['mode']}",
            f"真实调用：{total.used}；硬检查统计：{counts}",
            "",
            "语义质量尚未验收；脚本化输出只验证运行与检查链路。",
            "",
            "| 用例 | 轮次 | 模式 | 硬检查 |",
            "| --- | --- | --- | --- |",
        ]
        table += [f"| {r['id']} | {r['repeat']} | {r['mode']} | {r['status']} |" for r in rows]
        (output / "report.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    return output, summary


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Offline eval defaults to scripted contract checks; "
            "--live explicitly enables billed model calls."
        )
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/reading-v1.json")
    parser.add_argument("--output", type=Path, default=ROOT / "evals/reports")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--fixtures", type=Path, help="Fixed topics/scripts directory")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-total-model-calls", type=int, default=100)
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args()
    try:
        output, summary = asyncio.run(
            run(
                args.dataset,
                args.output,
                live=args.live,
                repeat=args.repeat,
                maximum_calls=args.max_total_model_calls,
                ids=args.case,
                profile=args.profile,
                fixtures=args.fixtures,
            )
        )
    except Exception as exc:
        # 配置校验异常可能携带原始配置值，CLI只输出异常类型。
        print(json.dumps({"error": "eval_startup_or_report_error", "type": type(exc).__name__}))
        raise SystemExit(2) from None
    print(
        json.dumps(
            {"report": str(output / "report.md"), "summary": summary}, ensure_ascii=False, indent=2
        )
    )
    if (
        summary["counts"].get("failed")
        or summary["counts"].get("error")
        or summary["counts"].get("skipped")
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
