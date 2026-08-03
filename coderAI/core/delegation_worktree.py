"""Isolated Git worktrees and conflict-safe delegated patch integration."""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Literal, Optional

from coderAI.system.fsperms import OWNER_RWX, restrict_path


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_INTERNAL_ONLY_PATHS = frozenset({".coderAI/tasks.json"})
MAX_DELEGATION_FILE_BYTES = 25 * 1024 * 1024
MAX_DELEGATION_SNAPSHOT_BYTES = 250 * 1024 * 1024
MAX_DELEGATION_PATCH_BYTES = 1024 * 1024
MAX_DELEGATION_PATCH_FILES = 250


class DelegationWorktreeError(RuntimeError):
    """Raised when isolation or patch integration cannot fail closed."""


@dataclass(frozen=True)
class FileSnapshot:
    """Stable state for one repository-relative path."""

    kind: Literal["missing", "file", "symlink"]
    sha256: Optional[str] = None
    mode: Optional[int] = None
    link_target: Optional[str] = None


@dataclass(frozen=True)
class DelegatedChange:
    """One exact file change relative to the delegation baseline."""

    path: str
    operation: Literal["added", "modified", "deleted"]
    before: FileSnapshot
    after: FileSnapshot


@dataclass(frozen=True)
class PreparedDelegatedPatch:
    """Reviewable immutable description of a child workspace patch."""

    worktree_id: str
    changes: tuple[DelegatedChange, ...]
    preview: str
    fingerprint: str


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "PATH", "SYSTEMROOT", "TMPDIR", "TMP", "TEMP"}
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DelegationWorktreeError(f"Git worktree operation failed: {exc}") from exc


def _git_output(root: Path, *args: str) -> bytes:
    result = _run_git(root, *args)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise DelegationWorktreeError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _validate_relative_path(raw: str) -> str:
    if not raw or "\x00" in raw:
        raise DelegationWorktreeError("delegated patch contains an empty or NUL path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise DelegationWorktreeError(f"unsafe delegated patch path: {raw}")
    return relative.as_posix()


def _is_integrable(path: str) -> bool:
    return path not in _INTERNAL_ONLY_PATHS and not path.startswith(".coderAI/plans/")


def _read_regular_file(path: Path) -> tuple[bytes, int]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise DelegationWorktreeError(f"unsupported delegated workspace entry: {path}")
    if before.st_size > MAX_DELEGATION_FILE_BYTES:
        raise DelegationWorktreeError(
            f"delegated workspace file exceeds {MAX_DELEGATION_FILE_BYTES} bytes: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DELEGATION_FILE_BYTES:
                raise DelegationWorktreeError(f"delegated workspace file grew too large: {path}")
            chunks.append(chunk)
    finally:
        os.close(fd)
    after = path.lstat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise DelegationWorktreeError(f"delegated workspace file changed during snapshot: {path}")
    return b"".join(chunks), stat.S_IMODE(after.st_mode)


def _snapshot(path: Path) -> tuple[FileSnapshot, Optional[bytes]]:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return FileSnapshot("missing"), None
    if stat.S_ISLNK(entry.st_mode):
        target = os.readlink(path)
        digest = hashlib.sha256(target.encode("utf-8", errors="surrogateescape")).hexdigest()
        return FileSnapshot("symlink", sha256=digest, link_target=target), None
    data, mode = _read_regular_file(path)
    return FileSnapshot("file", hashlib.sha256(data).hexdigest(), mode), data


def _remove_leaf(path: Path) -> None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
        raise DelegationWorktreeError(f"refusing to replace directory entry: {path}")
    path.unlink()


def _write_file(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode & 0o777)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class DelegationWorktree:
    """A short-lived child workspace seeded from the parent's live state."""

    def __init__(
        self,
        *,
        parent_root: str | Path,
        storage_root: Optional[str | Path] = None,
        worktree_id: Optional[str] = None,
    ) -> None:
        self.parent_root = Path(parent_root).expanduser().resolve()
        identifier = worktree_id or f"delegation_{uuid.uuid4().hex}"
        if not _SAFE_ID.fullmatch(identifier):
            raise ValueError("worktree_id must be path-safe")
        self.worktree_id = identifier
        self.storage_root = (
            Path(storage_root).expanduser().resolve()
            if storage_root is not None
            else (Path.home() / ".coderAI" / "worktrees").resolve()
        )
        self.allocation_root = self.storage_root / identifier
        self.workspace_root = self.allocation_root / "workspace"
        self.baseline_root = self.allocation_root / "baseline"
        self._baseline: dict[str, FileSnapshot] = {}
        self._created = False

    def create(self) -> Path:
        """Create and seed a detached worktree from the parent's live files."""
        if self._created or self.allocation_root.exists():
            raise DelegationWorktreeError("delegation worktree already exists")
        top_level = Path(
            _git_output(self.parent_root, "rev-parse", "--show-toplevel")
            .decode("utf-8", errors="strict")
            .strip()
        ).resolve()
        if top_level != self.parent_root:
            raise DelegationWorktreeError(
                "mutating delegation requires the configured project root to equal the Git root"
            )
        self.storage_root.mkdir(parents=True, exist_ok=True)
        restrict_path(self.storage_root, OWNER_RWX)
        self.allocation_root.mkdir(mode=OWNER_RWX)
        restrict_path(self.allocation_root, OWNER_RWX)
        self.baseline_root.mkdir(mode=OWNER_RWX)
        restrict_path(self.baseline_root, OWNER_RWX)
        try:
            result = _run_git(
                self.parent_root,
                "worktree",
                "add",
                "--detach",
                str(self.workspace_root),
                "HEAD",
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()[:1000]
                raise DelegationWorktreeError(detail or "git worktree add failed")
            self._created = True
            self._seed_parent_state()
            return self.workspace_root
        except Exception:
            self.cleanup()
            raise

    def _listed_paths(self, root: Path) -> set[str]:
        raw = _git_output(
            root,
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        )
        paths: set[str] = set()
        for item in raw.split(b"\0"):
            if not item:
                continue
            try:
                decoded = item.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise DelegationWorktreeError(
                    "delegated worktrees require UTF-8 repository paths"
                ) from exc
            paths.add(_validate_relative_path(decoded))
        return paths

    def _seed_parent_state(self) -> None:
        total = 0
        for relative in sorted(self._listed_paths(self.parent_root)):
            source = self.parent_root / relative
            destination = self.workspace_root / relative
            baseline = self.baseline_root / relative
            state, data = _snapshot(source)
            self._baseline[relative] = state
            if state.kind == "missing":
                _remove_leaf(destination)
                continue
            if state.kind == "symlink":
                _remove_leaf(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(state.link_target or "", destination)
                continue
            assert data is not None and state.mode is not None
            total += len(data)
            if total > MAX_DELEGATION_SNAPSHOT_BYTES:
                raise DelegationWorktreeError(
                    "delegation snapshot exceeds the configured aggregate size ceiling"
                )
            _remove_leaf(destination)
            _write_file(destination, data, state.mode)
            _write_file(baseline, data, state.mode)

    def prepare_patch(self) -> PreparedDelegatedPatch:
        """Build an exact review diff without mutating the parent workspace."""
        if not self._created:
            raise DelegationWorktreeError("delegation worktree is not active")
        candidates = set(self._baseline) | self._listed_paths(self.workspace_root)
        changes: list[DelegatedChange] = []
        preview_parts: list[str] = []
        fingerprint = hashlib.sha256()
        for relative in sorted(path for path in candidates if _is_integrable(path)):
            before = self._baseline.get(relative, FileSnapshot("missing"))
            after, after_data = _snapshot(self.workspace_root / relative)
            if before == after:
                continue
            if len(changes) >= MAX_DELEGATION_PATCH_FILES:
                raise DelegationWorktreeError(
                    f"delegated patch exceeds {MAX_DELEGATION_PATCH_FILES} changed files"
                )
            if before.kind == "symlink" or after.kind == "symlink":
                raise DelegationWorktreeError(
                    f"delegated patch changes a symlink and cannot be integrated safely: {relative}"
                )
            operation: Literal["added", "modified", "deleted"]
            if before.kind == "missing":
                operation = "added"
            elif after.kind == "missing":
                operation = "deleted"
            else:
                operation = "modified"
            change = DelegatedChange(relative, operation, before, after)
            changes.append(change)
            fingerprint.update(
                f"{relative}\0{operation}\0{before.sha256}\0{after.sha256}\0{after.mode}".encode()
            )
            before_data = (
                (self.baseline_root / relative).read_bytes() if before.kind == "file" else b""
            )
            preview_parts.append(
                self._preview_change(change, before_data=before_data, after_data=after_data or b"")
            )
            if (
                sum(len(part.encode("utf-8")) for part in preview_parts)
                > MAX_DELEGATION_PATCH_BYTES
            ):
                raise DelegationWorktreeError(
                    "delegated patch review exceeds the preview size ceiling; split the task"
                )
        return PreparedDelegatedPatch(
            worktree_id=self.worktree_id,
            changes=tuple(changes),
            preview="\n".join(preview_parts),
            fingerprint=fingerprint.hexdigest(),
        )

    @staticmethod
    def _preview_change(
        change: DelegatedChange,
        *,
        before_data: bytes,
        after_data: bytes,
    ) -> str:
        mode_note = ""
        if change.before.mode != change.after.mode:
            mode_note = f"mode {change.before.mode!s} -> {change.after.mode!s}\n"
        try:
            if b"\0" in before_data or b"\0" in after_data:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary NUL")
            before_text = before_data.decode("utf-8").splitlines(keepends=True)
            after_text = after_data.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return (
                f"Binary {change.operation}: {change.path}\n"
                f"before sha256={change.before.sha256 or '-'}\n"
                f"after sha256={change.after.sha256 or '-'}\n"
                f"{mode_note}"
            )
        diff = "".join(
            difflib.unified_diff(
                before_text,
                after_text,
                fromfile=f"a/{change.path}",
                tofile=f"b/{change.path}",
            )
        )
        return mode_note + diff

    def integrate(self, prepared: PreparedDelegatedPatch) -> list[dict[str, str]]:
        """Apply an approved patch iff both parent and child still match review."""
        if prepared.worktree_id != self.worktree_id:
            raise DelegationWorktreeError("delegated patch belongs to a different worktree")
        current = self.prepare_patch()
        if current.fingerprint != prepared.fingerprint or current.changes != prepared.changes:
            raise DelegationWorktreeError("delegated workspace changed after patch review")

        materialized: list[tuple[DelegatedChange, Optional[bytes]]] = []
        for change in prepared.changes:
            parent_path = self._safe_parent_path(change.path)
            parent_state, _ = _snapshot(parent_path)
            if parent_state != change.before:
                raise DelegationWorktreeError(
                    f"parent workspace changed during delegation: {change.path}"
                )
            child_state, child_data = _snapshot(self.workspace_root / change.path)
            if child_state != change.after:
                raise DelegationWorktreeError(
                    f"delegated workspace changed after review: {change.path}"
                )
            materialized.append((change, child_data))

        applied: list[dict[str, str]] = []
        for change, child_data in materialized:
            destination = self._safe_parent_path(change.path)
            if change.operation == "deleted":
                _remove_leaf(destination)
            else:
                assert child_data is not None and change.after.mode is not None
                _remove_leaf(destination)
                _write_file(destination, child_data, change.after.mode)
            applied.append({"path": change.path, "operation": change.operation})
        return applied

    def _safe_parent_path(self, relative: str) -> Path:
        relative = _validate_relative_path(relative)
        candidate = self.parent_root / relative
        current = self.parent_root
        for part in PurePosixPath(relative).parts[:-1]:
            current = current / part
            try:
                entry = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry.st_mode):
                raise DelegationWorktreeError(
                    f"delegated patch path traverses a symlink: {relative}"
                )
        resolved_parent = candidate.parent.resolve()
        if not resolved_parent.is_relative_to(self.parent_root):
            raise DelegationWorktreeError(f"delegated patch escapes project root: {relative}")
        return candidate

    def cleanup(self) -> None:
        """Remove only this manager's validated generated worktree allocation."""
        if self.allocation_root.parent != self.storage_root:
            raise DelegationWorktreeError("refusing unsafe delegation worktree cleanup")
        if self.workspace_root.exists() or self.workspace_root.is_symlink():
            result = _run_git(
                self.parent_root,
                "worktree",
                "remove",
                "--force",
                str(self.workspace_root),
            )
            if result.returncode != 0:
                shutil.rmtree(self.workspace_root, ignore_errors=True)
                _run_git(self.parent_root, "worktree", "prune", "--expire", "now")
        if self.allocation_root.exists():
            shutil.rmtree(self.allocation_root)
        self._created = False
