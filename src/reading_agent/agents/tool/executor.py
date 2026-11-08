"""统一参数/角色检查、分发、缓存与本次读取凭据；不包含具体业务工具。"""

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..models import Coverage, Task
from ..permission import check_permission
from .base import error
from .corpus import Corpus
from .registry import REGISTRY


class ToolSession:
    def __init__(self, corpus: Corpus, task: Task):
        corpus.validate_task(task)
        self.corpus, self.task = corpus, task
        self.reads: list[Coverage] = []
        self.readable_ids = {task.material_id}
        self.memory_grants: dict[str, str] = {}
        self.session_grants: dict[tuple[str, str], dict] = {}
        self.cache: dict[str, dict[str, Any]] = {}
        self.summarize: Callable | None = None
        self.loaded_tools = set()
        self.recovered_outcomes = {}
        self.unknown_mutations = False
        self.is_resuming = False

    def _recovery_guard(self, spec, args):
        if not spec.side_effect:
            return None
        if self.unknown_mutations:
            return error("previous_execution_unknown_requires_reconciliation")
        if not self.is_resuming:
            return None
        from ..recovery import fingerprint

        saved = self.recovered_outcomes.get(fingerprint(spec.name, args.model_dump_json()))
        if not saved:
            return None
        if saved.get("safe_to_retry"):
            return None
        if saved.get("unknown"):
            return error("previous_execution_unknown_requires_reconciliation")
        response = saved["result"]
        if spec.name in {"write_file", "edit_file"} and response["status"] == "success":
            try:
                workspace = self.corpus.workspace
                with workspace.lock():
                    _, digest = workspace.read(workspace.path(args.path))
                if digest != response["data"]["sha256"]:
                    return error("recovered_file_version_conflict")
            except (OSError, ValueError):
                return error("recovered_file_version_conflict")
        return {**response, "replayed_result": True}

    @property
    def eligible_names(self):
        return [
            name
            for name, spec in REGISTRY.items()
            if self.task.role in spec.roles
            and (spec.available is None or spec.available(self))
            and check_permission(name, None, self.corpus.permission_policy) is None
        ]

    @property
    def names(self):
        eligible = self.eligible_names
        if "search_tool" not in eligible:
            return eligible
        core = {"material_read", "read_file", "search_tool"}
        return [name for name in eligible if name in core or name in self.loaded_tools]

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": REGISTRY[name].api_description,
                    "parameters": REGISTRY[name].arguments.model_json_schema(),
                    "strict": True,
                },
            }
            for name in self.names
        ]

    @staticmethod
    def error(code):
        return error(code)

    def _prepare(self, name, arguments):
        if name not in REGISTRY:
            return None, None, error("tool_not_allowed")
        spec = REGISTRY[name]
        try:
            args = spec.arguments.model_validate_json(arguments)
        except (ValidationError, ValueError):
            return (
                None,
                None,
                error("invalid_arguments" if name in self.names else "tool_not_allowed"),
            )
        denied = check_permission(name, args, self.corpus.permission_policy)
        if denied:
            return None, None, error(denied)
        if name not in self.names:
            return None, None, error("tool_not_allowed")
        return spec, args, None

    def _cached(self, spec, key):
        if not spec.cacheable or key not in self.cache:
            return None
        response = self.cache[key]
        if spec.validate_cache is not None and not spec.validate_cache(self, response):
            del self.cache[key]
            return None
        return response

    def _remember(self, spec, key, response):
        if spec.cacheable and response["status"] == "success":
            self.cache[key] = response
        return response

    def execute(self, name, arguments):
        spec, args, failure = self._prepare(name, arguments)
        if failure is not None:
            return failure
        recovered = self._recovery_guard(spec, args)
        if recovered is not None:
            return recovered
        if spec.run is None:
            return error("async_tool_required")
        key = name + args.model_dump_json()
        response = self._cached(spec, key)
        if response is not None:
            return response
        return self._remember(spec, key, spec.run(self, args))

    async def aexecute(self, name, arguments):
        spec, args, failure = self._prepare(name, arguments)
        if failure is not None:
            return failure
        recovered = self._recovery_guard(spec, args)
        if recovered is not None:
            return recovered
        key = name + args.model_dump_json()
        response = self._cached(spec, key)
        if response is not None:
            return response
        response = await spec.arun(self, args) if spec.arun is not None else spec.run(self, args)
        return self._remember(spec, key, response)

    def validate_final(self, final: Any) -> None:
        if final.status == "failed":
            return
        if self.task.role == "route":
            topic_ids = [item.topic_id for item in final.routes]
            if len(set(topic_ids)) != len(topic_ids):
                raise ValueError("duplicate routed Topic")
            for item in final.routes:
                if item.topic_id not in self.task.topic_ids:
                    raise ValueError("routed Topic outside candidates")
                legal = {tag.id for tag in self.corpus.topics[item.topic_id].tags}
                if not set(item.tag_ids) <= legal:
                    raise ValueError("tag outside routed Topic")
        elif self.task.role == "deep_read":
            if not self.reads:
                raise ValueError("deep reading requires actual body reads")
            for claim in final.coverage:
                if claim.material_id != self.task.material_id:
                    raise ValueError("coverage must reference the current material")
            body = self.corpus.materials[self.task.material_id].body or ""

            # 比较实际字符集合，允许合并相邻片段或报告等价分段；不能跨未读缺口。
            def merged(ranges):
                result = []
                for read in sorted(ranges, key=lambda r: (r.start, r.end)):
                    if result and read.start <= result[-1][1]:
                        result[-1][1] = max(result[-1][1], read.end)
                    else:
                        result.append([read.start, read.end])
                return result

            if merged(final.coverage) != merged(self.reads):
                raise ValueError(
                    "coverage differs from actual read characters (gap, omission or excess)"
                )
            cursor = 0
            for read in sorted(self.reads, key=lambda r: r.start):
                if read.start > cursor:
                    break
                cursor = max(cursor, read.end)
            if cursor < len(body) and not final.limitations.strip():
                raise ValueError("partial reading must disclose limitations")

    def readable_log(self) -> list[dict[str, Any]]:
        return [r.model_dump() for r in self.reads]
