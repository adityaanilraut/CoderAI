"""Workspace trust boundary for CoderAI (Phase 2 of the security hardening plan).

Opening or cloning a repository is treated as *untrusted* until the user makes
an explicit trust decision. Until then, none of the repo's execution/config
surface — ``.coderAI/hooks.json``, the project ``config.json`` overlay, project
rules/skills and ``permission.ask`` auto-allow — is honoured. This stops a
freshly cloned malicious repo from driving privileged local execution on first
contact (threat model: untrusted repo input must never reach the shell / file /
network layers without a human trust decision).

Trust is keyed by the **resolved absolute project root** and pinned to a
``fingerprint`` over the security-relevant inputs. If any of those files change
after the folder was trusted the fingerprint no longer matches and the folder
is treated as untrusted again (re-prompt) — so an attacker cannot get a benign
checkout trusted and then swap in a malicious hook.

Fail-closed: any error reading the store, a missing entry, or a fingerprint
mismatch all yield "untrusted".

The store lives at ``<config_dir>/trusted_folders.json`` (0600, written
atomically). Reading the path from ``config_manager.config_dir`` lazily means
the test ``isolated_home`` fixture (which re-points that dir) transparently
sandboxes the trust store too.

Escape hatch: setting ``CODERAI_TRUST_WORKSPACE`` truthy trusts every workspace
for the process. This backs the documented, dangerous ``--trust-workspace``
headless/CI flag and is what the test suite uses to keep the pre-existing
fixtures (which build ``.coderAI`` trees in tmp dirs) working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List

from coderAI.system.fsperms import atomic_write_json

logger = logging.getLogger(__name__)

# Name of the trust store inside the CoderAI config dir.
_STORE_NAME = "trusted_folders.json"

# Fixed files (relative to the project root) whose contents can drive local
# execution or relax the security posture. ``hooks.json`` also contains the
# ``permission.ask`` hook definitions.
_FINGERPRINT_FILES = (
    ".coderAI/hooks.json",
    ".coderAI/config.json",
)

_FINGERPRINT_TREES = (
    ".coderAI/rules",
    ".coderAI/skills",
    ".coderAI/agents",
)

# Project guidance is deliberately bounded. Refuse to trust a tree that cannot
# be completely fingerprinted rather than silently omitting attacker-controlled
# content from the trust decision.
_MAX_FINGERPRINT_FILES = 1_024
_MAX_FINGERPRINT_BYTES = 10 * 1024 * 1024
_MAX_FINGERPRINT_DEPTH = 8
_READ_CHUNK_BYTES = 64 * 1024


def _env_trusts_all() -> bool:
    """True when ``CODERAI_TRUST_WORKSPACE`` opts the whole process into trust."""
    val = os.getenv("CODERAI_TRUST_WORKSPACE", "")
    return val.strip().lower() in ("1", "true", "yes", "on")


class WorkspaceTrust:
    """Tracks which project roots the user has explicitly trusted.

    A module-level singleton (:data:`workspace_trust`) is the intended entry
    point; the class is instantiable for tests.
    """

    def __init__(self) -> None:
        # (mtime_ns, parsed folders dict) cache so the hot ``is_trusted`` path
        # (called per turn) doesn't re-read + re-parse the store every time.
        self._cache: tuple[int, Dict[str, Any]] | None = None
        self._pinned_decision: ContextVar[tuple[str, bool] | None] = ContextVar(
            f"workspace_trust_decision_{id(self)}", default=None
        )

    # ── paths ────────────────────────────────────────────────────────────────

    def _store_path(self) -> Path:
        # Imported lazily to avoid an import cycle (config imports trust) and so
        # a test re-pointing ``config_manager.config_dir`` takes effect here.
        from coderAI.system.config import config_manager

        return Path(config_manager.config_dir) / _STORE_NAME

    @staticmethod
    def _resolve(root: Any) -> str:
        """Resolve *root* (str / Path / os.PathLike) to an absolute string key."""
        return str(Path(os.fspath(root)).resolve())

    # ── store I/O ────────────────────────────────────────────────────────────

    def _load_store(self) -> Dict[str, Any]:
        """Return the ``{resolved_root: entry}`` map, or ``{}`` on any error."""
        path = self._store_path()
        try:
            stat = path.stat()
        except OSError:
            self._cache = None
            return {}
        cached = self._cache
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Failed to read trust store %s: %s", path, e)
            return {}  # fail closed → untrusted
        folders = data.get("folders") if isinstance(data, dict) else None
        result: Dict[str, Any] = folders if isinstance(folders, dict) else {}
        self._cache = (stat.st_mtime_ns, result)
        return result

    def _save_store(self, folders: Dict[str, Any]) -> None:
        path = self._store_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"version": 1, "folders": folders}
        atomic_write_json(path, payload)
        # Invalidate cache so the next read reflects the write.
        self._cache = None

    # ── fingerprint / surface detection ──────────────────────────────────────

    @staticmethod
    def _open_regular_file(path: Path) -> BinaryIO:
        """Open *path* without following a final symlink, or fail closed."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"Trust fingerprint only supports regular files: {path}")
            return os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            raise

    @classmethod
    def _hash_file(
        cls,
        h: Any,
        path: Path,
        rel: str,
        *,
        total_bytes: int,
    ) -> int:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            h.update(b"absent\0")
            return total_bytes
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlinks are not allowed in trusted workspace inputs: {rel}")
        if not stat.S_ISREG(mode):
            raise ValueError(f"Unsupported trusted workspace input: {rel}")

        h.update(b"file\0")
        with cls._open_regular_file(path) as file_obj:
            while chunk := file_obj.read(_READ_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > _MAX_FINGERPRINT_BYTES:
                    raise ValueError(
                        "Trusted workspace inputs exceed the fingerprint byte limit "
                        f"({_MAX_FINGERPRINT_BYTES} bytes)"
                    )
                h.update(chunk)
        return total_bytes

    @classmethod
    def _tree_files(cls, root: Path, rel_root: str) -> List[tuple[str, Path]]:
        """Return every regular tree file in deterministic, symlink-safe order."""
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlinks are not allowed in trusted workspace inputs: {rel_root}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"Trusted workspace input must be a directory: {rel_root}")

        files: List[tuple[str, Path]] = []
        pending: List[tuple[Path, str, int]] = [(root, rel_root, 0)]
        while pending:
            directory, rel_dir, depth = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as e:
                raise ValueError(f"Cannot scan trusted workspace input {rel_dir}: {e}") from e
            child_dirs: List[tuple[Path, str, int]] = []
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}"
                try:
                    if entry.is_symlink():
                        raise ValueError(
                            f"Symlinks are not allowed in trusted workspace inputs: {rel}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        if depth >= _MAX_FINGERPRINT_DEPTH:
                            raise ValueError(
                                "Trusted workspace inputs exceed the fingerprint depth limit "
                                f"({_MAX_FINGERPRINT_DEPTH}): {rel}"
                            )
                        child_dirs.append((Path(entry.path), rel, depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        files.append((rel, Path(entry.path)))
                        if len(files) > _MAX_FINGERPRINT_FILES:
                            raise ValueError(
                                "Trusted workspace inputs exceed the fingerprint file limit "
                                f"({_MAX_FINGERPRINT_FILES})"
                            )
                    else:
                        raise ValueError(f"Unsupported trusted workspace input: {rel}")
                except OSError as e:
                    raise ValueError(f"Cannot inspect trusted workspace input {rel}: {e}") from e
            # Reverse push preserves ascending traversal when using a LIFO stack.
            pending.extend(reversed(child_dirs))
        return sorted(files, key=lambda item: item[0])

    def fingerprint(self, root: Any) -> str:
        """SHA-256 over the security-relevant repo inputs at *root*.

        Missing files and trees hash as sentinels, so adding any project config,
        hook, rule, skill, or persona changes the fingerprint and forces a
        re-prompt. Trees are bounded and never follow symlinks; inputs that
        cannot be completely fingerprinted are rejected instead of partially
        trusted.
        """
        resolved = Path(self._resolve(root))
        h = hashlib.sha256()
        total_bytes = 0
        total_files = 0
        for rel in _FINGERPRINT_FILES:
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            total_bytes = self._hash_file(
                h,
                resolved / rel,
                rel,
                total_bytes=total_bytes,
            )
            h.update(b"\0")
        for rel_root in _FINGERPRINT_TREES:
            h.update(rel_root.encode("utf-8"))
            h.update(b"\0")
            tree_root = resolved / rel_root
            files = self._tree_files(tree_root, rel_root)
            total_files += len(files)
            if total_files > _MAX_FINGERPRINT_FILES:
                raise ValueError(
                    "Trusted workspace inputs exceed the fingerprint file limit "
                    f"({_MAX_FINGERPRINT_FILES})"
                )
            if not files:
                h.update(b"absent-or-empty\0")
                continue
            for rel, path in files:
                h.update(rel.encode("utf-8"))
                h.update(b"\0")
                total_bytes = self._hash_file(
                    h,
                    path,
                    rel,
                    total_bytes=total_bytes,
                )
                h.update(b"\0")
        return h.hexdigest()

    def has_execution_surface(self, root: Any) -> bool:
        """True when *root* carries any ``.coderAI`` surface worth prompting on.

        Used to decide whether the first-run trust prompt should appear at all —
        a plain repo with no automation should not nag the user.
        """
        try:
            dot = Path(self._resolve(root)) / ".coderAI"
        except (OSError, ValueError):
            return False
        if not dot.is_dir():
            return False
        if (dot / "hooks.json").is_file() or (dot / "config.json").is_file():
            return True
        for sub in ("rules", "skills", "agents"):
            d = dot / sub
            try:
                if d.is_dir() and any(d.iterdir()):
                    return True
            except OSError:
                continue
        return False

    # ── public API ───────────────────────────────────────────────────────────

    @contextmanager
    def pinned_decision(self, root: Any, trusted: bool) -> Iterator[None]:
        """Temporarily reuse one trust decision for nested synchronous loads."""
        resolved = self._resolve(root)
        token = self._pinned_decision.set((resolved, trusted))
        try:
            yield
        finally:
            self._pinned_decision.reset(token)

    def is_trusted(self, root: Any) -> bool:
        """True iff *root* has an explicit, fingerprint-matching trust record.

        The ``CODERAI_TRUST_WORKSPACE`` env override short-circuits to trusted.
        """
        try:
            resolved = self._resolve(root)
        except (OSError, ValueError):
            return False
        pinned = self._pinned_decision.get()
        if pinned is not None and pinned[0] == resolved:
            return pinned[1]
        if _env_trusts_all():
            return True
        entry = self._load_store().get(resolved)
        if not isinstance(entry, dict):
            return False
        try:
            return entry.get("fingerprint") == self.fingerprint(resolved)
        except (OSError, ValueError) as e:
            logger.debug("Workspace trust fingerprint failed for %s: %s", resolved, e)
            return False

    def record_trust(self, root: Any, trusted_by: str = "user") -> None:
        """Persist an explicit trust decision for *root* at its current state."""
        resolved = self._resolve(root)
        folders = dict(self._load_store())
        folders[resolved] = {
            "fingerprint": self.fingerprint(resolved),
            "trusted_at": time.time(),
            "trusted_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trusted_by": trusted_by,
        }
        self._save_store(folders)

    def revoke_trust(self, root: Any) -> bool:
        """Drop any trust record for *root*. Returns True if one was removed."""
        resolved = self._resolve(root)
        folders = dict(self._load_store())
        if resolved in folders:
            del folders[resolved]
            self._save_store(folders)
            return True
        return False

    def trusted_folders(self) -> List[str]:
        """Resolved roots that currently have a trust record (unfiltered)."""
        return sorted(self._load_store().keys())


# Module-level singleton — the intended entry point.
workspace_trust = WorkspaceTrust()
