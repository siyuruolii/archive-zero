"""统一执行前权限检查：敏感路径优先，其次配置拒绝，最后权限等级。"""

import fnmatch
import re
from typing import Literal

from pydantic import Field

from .models import StrictModel


class PermissionPolicy(StrictModel):
    level: Literal["read", "write", "execute"] = "execute"
    denied_tools: list[str] = Field(default_factory=list)
    allowed_tools: list[str] | None = None
    denied_paths: list[str] = Field(default_factory=list)
    allow_host_shell: bool = True
    allow_container: bool = True
    allow_srt: bool = True


SENSITIVE = re.compile(
    r"(?i)(?:^|[/\\\s'\"])(?:\.env(?:\.[^/\\\s]*)?|\.ssh|\.aws|\.azure|\.codex|"
    r"\.git|\.local|models?\.json|reading\.db|id_rsa|id_ed25519|credentials)"
    r"(?:$|[/\\\s'\"])",
)


def check_permission(name, args, policy=None):
    policy = policy or PermissionPolicy()
    values = args.model_dump() if args is not None else {}
    paths = [str(value) for key, value in values.items() if key in {"path", "file_path"}]
    for key in ("paths", "file_paths"):
        paths.extend(str(value) for value in values.get(key, []))
    if name == "bash":
        paths.append(values.get("command", ""))
    # allow-list、权限等级、工具发现和用户同意都不能覆盖明确的敏感路径拒绝。
    if any(SENSITIVE.search(path.replace("\\", "/")) for path in paths):
        return "sensitive_path_denied"
    if any(
        fnmatch.fnmatchcase(path.replace("\\", "/").casefold(), rule.replace("\\", "/").casefold())
        for path in paths
        for rule in policy.denied_paths
    ):
        return "configured_path_denied"
    if name in policy.denied_tools:
        return "configured_tool_denied"
    if policy.allowed_tools is not None and name not in policy.allowed_tools:
        return "tool_not_in_allowlist"
    required = (
        "execute"
        if name == "bash"
        else "write"
        if name
        in {
            "write_file",
            "edit_file",
            "memory_file_write",
            "memory_file_patch",
            "memory_file_create",
        }
        else "read"
    )
    levels = {"read": 0, "write": 1, "execute": 2}
    if levels[policy.level] < levels[required]:
        return "permission_level_denied"
    return None
