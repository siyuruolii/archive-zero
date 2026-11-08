"""离线检查文档地图和层间导入；不读取用户数据或调用模型。"""

import ast
import re
from collections import deque
from importlib.util import resolve_name
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def check_docs(root):
    documents = {root / "README.md", root / "AGENTS.md", root / "evals/README.md"}
    documents.update((root / "docs").rglob("*.md"))
    errors, edges = [], {}
    for path in sorted(documents):
        label = path.relative_to(root).as_posix()
        if not path.is_file():
            errors.append(f"{label}: missing documentation entry")
            continue
        content = path.read_text(encoding="utf-8")
        if path.name.casefold() in {"interview.md", "resume.md"}:
            errors.append(f"{label}: personal preparation belongs outside the repository")
        if path.name == "AGENTS.md" and len(content.splitlines()) > 100:
            errors.append(f"{label}: keep entry map within 100 lines; move details to docs")
        content = re.sub(r"```.*?```", "", content, flags=re.S)
        edges[path] = []
        for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", content):
            target = target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.is_relative_to(root):
                errors.append(f"{label}: local documentation link escapes repository: {target}")
            elif not destination.exists():
                errors.append(f"{label}: broken local link: {target}")
            elif destination in documents:
                edges[path].append(destination)
    visited, pending = set(), deque([root / "README.md", root / "AGENTS.md"])
    while pending:
        path = pending.popleft()
        if path not in visited:
            visited.add(path)
            pending.extend(edges.get(path, []))
    for path in sorted(documents - visited):
        errors.append(f"{path.relative_to(root)}: orphan document; link from the repository map")
    return errors


def check_imports(root):
    package = root / "src/reading_agent"
    forbidden = {
        "agents": {"services", "interfaces"},
        "services": {"interfaces"},
        "storage": {"agents", "services", "interfaces", "infrastructure"},
        "infrastructure": {"agents", "services", "interfaces"},
    }
    errors = []
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package)
        layer = relative.parts[0]
        banned = forbidden.get(layer, set())
        parent = ".".join(["reading_agent", *relative.parts[:-1]])
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, parent)
                targets = [*(f"{module}.{alias.name}" for alias in node.names), module]
            for target in targets:
                if layer == "services" and any(
                    target == module or target.startswith(module + ".")
                    for module in (
                        "reading_agent.agents.loop",
                        "reading_agent.agents.context",
                        "reading_agent.agents.inference",
                        "reading_agent.agents.compact",
                    )
                ):
                    errors.append(
                        f"{path.relative_to(root)}:{node.lineno}: "
                        f"services must use Harness: {target}"
                    )
                    break
                # 现有配置适配器从领域模型解析配置；仅此文件可接线这三个模块。
                wiring = {
                    "reading_agent.agents.provider",
                    "reading_agent.agents.model_router",
                    "reading_agent.services.memory_consolidation",
                }
                if relative.as_posix() == "infrastructure/configuration.py" and any(
                    target == module or target.startswith(module + ".") for module in wiring
                ):
                    continue
                parts = target.split(".")
                if len(parts) >= 2 and parts[0] == "reading_agent" and parts[1] in banned:
                    errors.append(
                        f"{path.relative_to(root)}:{node.lineno}: {layer} cannot import "
                        f"{target}; use Harness/callback boundaries"
                    )
                    break
    return errors


def main():
    errors = [*check_docs(ROOT), *check_imports(ROOT)]
    for error in errors:
        print(error)
    if not errors:
        print("Repository map, local links and import boundaries: passed.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
