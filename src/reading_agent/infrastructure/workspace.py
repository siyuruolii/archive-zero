"""显式授权的专用工作区；不接受模型指定根目录。"""

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .locking import FileLock


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class Workspace:
    root: Path
    image: str | None = None

    def __post_init__(self):
        root = Path(self.root).absolute()
        object.__setattr__(self, "root", root)
        root.mkdir(parents=True, exist_ok=True)
        self.check_links(root)

    @staticmethod
    def check_links(path):
        for part in (path, *path.parents):
            if not part.exists() and not part.is_symlink():
                continue
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkspaceError("links_not_allowed")

    def path(self, name):
        parts = name.replace("\\", "/").split("/")
        if len(parts) > 16 or any(
            not p
            or p.startswith(".")
            or p.endswith((".", " "))
            or re.search(r'[<>:"|?*\x00-\x1f]', p)
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", p)
            for p in parts
        ):
            raise WorkspaceError("invalid_workspace_path")
        path = self.root.joinpath(*parts)
        self.check_links(path)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WorkspaceError("path_outside_workspace")
        return path

    def lock(self):
        self.check_links(self.root / ".workspace.lock")
        return FileLock(self.root / ".workspace.lock")

    def read(self, path):
        self.check_links(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceError("regular_single_link_file_required")
        if info.st_size > 262144:
            raise WorkspaceError("file_size_limit")
        with path.open("rb") as stream:
            data = stream.read(262145)
        if len(data) > 262144:
            raise WorkspaceError("file_size_limit")
        text = data.decode("utf-8")
        if "\x00" in text:
            raise WorkspaceError("text_file_required")
        return text, hashlib.sha256(data).hexdigest()

    def write(self, path, content, expected):
        data = content.encode("utf-8")
        if len(data) > 262144 or "\x00" in content:
            raise WorkspaceError("file_size_or_format_limit")
        current = self.read(path)[1] if path.exists() else None
        if current != expected:
            raise WorkspaceError("file_version_conflict")
        size, count = 0, 0
        for candidate in self.root.rglob("*"):
            count += 1
            if count > 256:
                raise WorkspaceError("workspace_entry_limit")
            self.check_links(candidate)
            if candidate.is_file():
                size += candidate.stat().st_size
        old_size = path.stat().st_size if path.exists() else 0
        if size - old_size + len(data) > 4194304:
            raise WorkspaceError("workspace_size_limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".write-", delete=False
            ) as out:
                temporary = Path(out.name)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            self.check_links(path)
            if (self.read(path)[1] if path.exists() else None) != expected:
                raise WorkspaceError("file_version_conflict")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return hashlib.sha256(data).hexdigest()
