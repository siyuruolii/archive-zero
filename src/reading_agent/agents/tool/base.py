"""工具元信息与统一结果格式。"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import StrictModel


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: type[StrictModel]
    roles: tuple[str, ...]
    guideline: str = field(kw_only=True)
    run: Callable | None = None
    arun: Callable | None = None
    available: Callable | None = None
    cacheable: bool = True
    validate_cache: Callable | None = None
    side_effect: bool = True
    retry_errors: tuple[str, ...] = ()

    @property
    def api_description(self):
        return self.description + "\n使用指南：" + self.guideline


def error(code):
    return {"status": "error", "error": code, "data": None}


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
