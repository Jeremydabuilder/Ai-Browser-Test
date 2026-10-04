"""An authorized project workspace: a folder the user explicitly chose.

The Coder may *read* from it (a bounded listing and the contents of the files
the task names) and *propose* edits. Nothing is ever written until the user
presses "Apply" on a specific change set, which calls ``apply`` here. Every
path is resolved and checked to stay inside the root, symlinks that point
outside it are ignored, and size/count caps keep a huge repo from flooding a
model call.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from app.team.agents import OutputError, safe_relative_path

IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea",
                ".mypy_cache", ".pytest_cache", "dist", "build", ".tox"}
MAX_LIST = 300
MAX_READ_CHARS = 60_000
MAX_STAGE_FILES = 400
MAX_STAGE_BYTES = 4 * 1024 * 1024
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".js",
                 ".ts", ".tsx", ".jsx", ".css", ".html", ".sh", ".rs", ".go", ".java", ".c", ".h",
                 ".cpp", ".csv", ".sql"}


class WorkspaceError(Exception):
    pass


@dataclass
class Change:
    path: str
    content: str
    is_new: bool
    diff: str


class Workspace:
    def __init__(self, root: str) -> None:
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"{root!r} is not a folder")
        self.root = resolved

    def resolve(self, rel: str) -> Path:
        try:
            clean = safe_relative_path(rel)
        except OutputError as exc:
            raise WorkspaceError(str(exc)) from exc
        target = (self.root / clean).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkspaceError(f"{rel!r} is outside the workspace")
        return target

    def list_files(self, limit: int = MAX_LIST) -> list[str]:
        found: list[str] = []
        for current, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS
                             and not os.path.islink(os.path.join(current, d)))
            for name in sorted(files):
                full = Path(current, name)
                if full.is_symlink() or full.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                found.append(full.relative_to(self.root).as_posix())
                if len(found) >= limit:
                    return found
        return found

    def read(self, rel: str, max_chars: int = MAX_READ_CHARS) -> str | None:
        try:
            target = self.resolve(rel)
        except WorkspaceError:
            return None
        if not target.is_file() or target.is_symlink():
            return None
        try:
            return target.read_text(encoding="utf-8", errors="replace")[:max_chars]
        except OSError:
            return None

    def snapshot(self) -> dict[str, str]:
        """Bounded copy of the workspace's text files, for the sandbox."""
        files: dict[str, str] = {}
        total = 0
        for rel in self.list_files(MAX_STAGE_FILES):
            text = self.read(rel, MAX_READ_CHARS * 4)
            if text is None:
                continue
            total += len(text)
            if total > MAX_STAGE_BYTES:
                break
            files[rel] = text
        return files

    def propose(self, path: str, content: str) -> Change:
        existing = self.read(path, MAX_READ_CHARS * 4)
        is_new = existing is None
        diff = "".join(difflib.unified_diff(
            (existing or "").splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile="/dev/null" if is_new else f"a/{path}", tofile=f"b/{path}", n=3))
        return Change(path, content, is_new, diff)

    def apply(self, changes: list[tuple[str, str]]) -> list[str]:
        """Write approved changes. Returns the paths written; raises
        WorkspaceError (before writing anything) if any path is unsafe."""
        targets = [(self.resolve(path), path, content) for path, content in changes]
        written: list[str] = []
        for target, path, content in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".pybrowser-tmp")
            temp.write_text(content, encoding="utf-8", newline="")
            os.replace(temp, target)
            written.append(path)
        return written
