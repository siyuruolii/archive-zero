"""开发验证入口；没有离线假模型冒充真实 Agent。"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

import httpx

from reading_agent.agents.harness import Budget, Event, Harness
from reading_agent.agents.model_router import ModelRouter
from reading_agent.agents.models import Task
from reading_agent.agents.sample import sample_corpus
from reading_agent.infrastructure.configuration import load_catalog


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Archive Zero（零号档案）开发验证入口")
    result.add_argument("role", choices=["route", "deep_read", "discuss"])
    result.add_argument(
        "--material", choices=["tools", "short", "missing", "injection"], default="tools"
    )
    result.add_argument("--topic", choices=["learn", "engineering"], action="append")
    result.add_argument("--question", default="")
    result.add_argument("--guideline", default="解释工具循环的分工、权限与有限重试边界。")
    result.add_argument("--preview", action="store_true", help="只检查并展示上下文，不请求模型")
    result.add_argument("--trace", action="store_true", help="在 stderr 展示草稿片段和工具结果")
    result.add_argument("--max-calls", type=int, default=6)
    result.add_argument("--max-tools", type=int, default=8)
    result.add_argument("--max-seconds", type=float, default=90.0)
    return result


async def execute(args) -> int:
    try:
        corpus = sample_corpus()
        task = Task(
            role=args.role,
            material_id=args.material,
            topic_ids=args.topic
            or (["learn", "engineering"] if args.role == "route" else ["learn"]),
            question=args.question
            or ("请结合材料解释工具循环。" if args.role == "discuss" else ""),
            deep_read_guideline=args.guideline if args.role == "deep_read" else "",
        )
        budget = Budget(
            max_model_calls=args.max_calls,
            max_tool_calls=args.max_tools,
            max_seconds=args.max_seconds,
        )
        if args.preview:
            print(
                json.dumps(
                    Harness(None, corpus, budget).prepare(task).messages,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        catalog = load_catalog()
    except (ValueError, OSError):
        # Pydantic 错误可能带 input_value，绝不能直接打印含密钥的异常。
        print(
            "配置无效：检查 Topic/预算、桌面模型设置或 AGENT_API_KEY、AGENT_MODEL、AGENT_BASE_URL。"
            "占位符不可用于真实请求，可用 --preview 离线检查。",
            file=sys.stderr,
        )
        return 2

    def show(event: Event) -> None:
        if args.trace:
            print(json.dumps(asdict(event), ensure_ascii=False), file=sys.stderr, flush=True)

    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        report = await Harness(ModelRouter(catalog, client), corpus, budget).run(
            task,
            on_event=show,
        )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0 if report.status == "succeeded" else 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(execute(parser().parse_args())))
    except KeyboardInterrupt:
        print("运行已中断；未保存输出，不自动重放请求。", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
