"""文件工具按领域集中；仅在程序注入专用工作区时开放。"""

from pydantic import Field

from reading_agent.infrastructure.workspace import WorkspaceError
from reading_agent.storage.database import Conflict

from ..models import StrictModel
from .base import ToolSpec, error


class ReadArgs(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    offset: int = Field(ge=0)
    length: int = Field(ge=1, le=8000)


class WriteArgs(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=262144)
    expected_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")


class EditArgs(StrictModel):
    path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=262144)
    new_text: str = Field(max_length=262144)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def available(session):
    return session.corpus.workspace is not None


def perform(session, args, operation):
    workspace = session.corpus.workspace
    try:
        with workspace.lock():
            path = (
                workspace.root
                if isinstance(args, ReadArgs) and args.path == "."
                else workspace.path(args.path)
            )
            return {"status": "success", "data": operation(workspace, path), "error": None}
    except WorkspaceError as exc:
        return error(str(exc))
    except Conflict:
        return error("workspace_busy")
    except FileNotFoundError:
        return error("file_not_found")
    except UnicodeError:
        return error("utf8_text_required")
    except OSError:
        return error("filesystem_error")


def read_file(session, args):
    def read(workspace, path):
        if path.is_dir():
            entries = sorted(
                (entry for entry in path.iterdir() if not entry.name.startswith(".")),
                key=lambda entry: entry.name.casefold(),
            )
            return {
                "path": args.path,
                "entries": [
                    entry.name
                    for entry in entries[args.offset : args.offset + min(args.length, 100)]
                ],
                "total_entries": len(entries),
                "truncated": args.offset + min(args.length, 100) < len(entries),
            }
        text, digest = workspace.read(path)
        end = min(len(text), args.offset + args.length)
        return {
            "path": args.path,
            "text": text[args.offset : end],
            "sha256": digest,
            "offset": args.offset,
            "end": end,
            "total_chars": len(text),
            "truncated": end < len(text),
        }

    return perform(session, args, read)


def write_file(session, args):
    return perform(
        session,
        args,
        lambda workspace, path: {
            "path": args.path,
            "sha256": workspace.write(path, args.content, args.expected_sha256),
        },
    )


def edit_file(session, args):
    def edit(workspace, path):
        text, digest = workspace.read(path)
        if digest != args.expected_sha256:
            raise WorkspaceError("file_version_conflict")
        if text.count(args.old_text) != 1:
            raise WorkspaceError("edit_requires_unique_match")
        return {
            "path": args.path,
            "sha256": workspace.write(path, text.replace(args.old_text, args.new_text, 1), digest),
        }

    return perform(session, args, edit)


TOOLS = (
    ToolSpec(
        "read_file",
        "分段读取专用工作区中的 UTF-8 文本，返回版本哈希。",
        ReadArgs,
        ("discuss",),
        side_effect=False,
        retry_errors=("transient_network_error", "tool_timeout", "workspace_busy"),
        guideline="只读取工作区相对路径；offset/length 为字符数，修改前先读取取得 sha256。",
        run=read_file,
        available=available,
        cacheable=False,
    ),
    ToolSpec(
        "write_file",
        "原子创建或覆盖工作区文本文件。",
        WriteArgs,
        ("discuss",),
        guideline=(
            "创建时 expected_sha256=null；覆盖必须提供读取时的哈希。不得存储密钥或擅自改写记忆。"
        ),
        run=write_file,
        available=available,
        cacheable=False,
    ),
    ToolSpec(
        "edit_file",
        "校验版本后精确替换唯一匹配文本。",
        EditArgs,
        ("discuss",),
        guideline="修改前读取；old_text 必须恰好匹配一次，冲突时重新读取后决定，不盲目重试覆盖。",
        run=edit_file,
        available=available,
        cacheable=False,
    ),
)
