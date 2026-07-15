"""OS-level sandbox backends for model- and repository-authored processes.

The sandbox is deliberately opt-in for compatibility. ``best_effort`` uses a
supported backend when one is usable and otherwise logs an explicit unconfined
fallback; ``required`` raises before spawning when confinement is unavailable.
Backends return argv wrappers rather than shell strings so the original command
is never interpolated into another layer of shell quoting.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Sequence, Tuple, cast

SandboxMode = Literal["off", "best_effort", "required"]

logger = logging.getLogger(__name__)


class SandboxUnavailableError(RuntimeError):
    """Raised when required OS confinement cannot be provided."""


@dataclass(frozen=True)
class SandboxLaunch:
    """A fully prepared subprocess argv and its truthful confinement status."""

    argv: List[str]
    sandboxed: bool
    backend: Optional[str] = None
    fallback_reason: Optional[str] = None


class SandboxBackend(ABC):
    """Interface implemented by one OS-specific argv wrapper."""

    name: str

    @abstractmethod
    def available(self) -> bool:
        """Return whether the backend exists and can launch a minimal process."""

    @abstractmethod
    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        allow_network: bool,
        temp_dirs: Sequence[Path],
    ) -> List[str]:
        """Return an argv that launches *argv* under this backend."""


def _probe(command: Tuple[str, ...]) -> bool:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=8)
def _probe_bubblewrap(executable: str) -> bool:
    true_path = shutil.which("true") or "/bin/true"
    return _probe(
        (
            executable,
            "--die-with-parent",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--",
            true_path,
        )
    )


class BubblewrapBackend(SandboxBackend):
    """Linux Bubblewrap backend using a read-only host root."""

    name = "bubblewrap"

    def __init__(self, executable: Optional[str] = None) -> None:
        self.executable = executable or shutil.which("bwrap") or ""

    def available(self) -> bool:
        return (
            sys.platform.startswith("linux")
            and bool(self.executable)
            and _probe_bubblewrap(self.executable)
        )

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        allow_network: bool,
        temp_dirs: Sequence[Path],
    ) -> List[str]:
        if not self.executable:
            raise SandboxUnavailableError("Bubblewrap executable was not found")

        wrapped = [
            self.executable,
            "--die-with-parent",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        if allow_network:
            wrapped.append("--share-net")

        # Writable temp space is intentional. Add the workspace last in case it
        # is itself nested below a temporary directory.
        for temp_dir in temp_dirs:
            wrapped.extend(("--bind", str(temp_dir), str(temp_dir)))
        wrapped.extend(("--bind", str(workspace), str(workspace)))
        wrapped.extend(("--chdir", str(cwd), "--"))
        wrapped.extend(argv)
        return wrapped


@lru_cache(maxsize=8)
def _probe_sandbox_exec(executable: str) -> bool:
    true_path = shutil.which("true") or "/usr/bin/true"
    return _probe((executable, "-p", "(version 1) (allow default)", true_path))


def _sbpl_path(path: Path) -> str:
    # SBPL accepts JSON-style quoted strings. Keeping this as one argv element
    # avoids shell quoting and profile injection through workspace names.
    return json.dumps(str(path))


class SandboxExecBackend(SandboxBackend):
    """macOS sandbox-exec backend with workspace/temp write exceptions."""

    name = "sandbox-exec"

    def __init__(self, executable: Optional[str] = None) -> None:
        self.executable = executable or shutil.which("sandbox-exec") or ""

    def available(self) -> bool:
        return (
            sys.platform == "darwin"
            and bool(self.executable)
            and _probe_sandbox_exec(self.executable)
        )

    def wrap(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        cwd: Path,
        allow_network: bool,
        temp_dirs: Sequence[Path],
    ) -> List[str]:
        del cwd  # sandbox-exec inherits cwd from create_subprocess_exec.
        if not self.executable:
            raise SandboxUnavailableError("sandbox-exec was not found")

        rules = ["(version 1)", "(allow default)", "(deny file-write*)"]
        if not allow_network:
            rules.append("(deny network*)")
        for device in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            rules.append(f"(allow file-write* (literal {json.dumps(device)}))")
        for path in (*temp_dirs, workspace):
            rules.append(f"(allow file-write* (subpath {_sbpl_path(path)}))")
        profile = " ".join(rules)
        return [self.executable, "-p", profile, *argv]


def _candidate_backends() -> List[SandboxBackend]:
    if sys.platform.startswith("linux"):
        return [BubblewrapBackend()]
    if sys.platform == "darwin":
        return [SandboxExecBackend()]
    return []


def select_backend(
    candidates: Optional[Iterable[SandboxBackend]] = None,
) -> Optional[SandboxBackend]:
    """Return the first usable backend for this host, if any."""
    for backend in candidates if candidates is not None else _candidate_backends():
        if backend.available():
            return backend
    return None


def _configured_settings() -> Tuple[SandboxMode, bool, Path]:
    try:
        from coderAI.core.services import get_services

        config = get_services().config
        mode = cast(SandboxMode, getattr(config, "sandbox_mode", "off"))
        allow_network = bool(getattr(config, "sandbox_allow_network", False))
        workspace = Path(getattr(config, "project_root", ".") or ".").expanduser().resolve()
        return mode, allow_network, workspace
    except Exception:
        logger.debug("sandbox config unavailable; defaulting to off", exc_info=True)
        return "off", False, Path.cwd().resolve()


def _writable_temp_dirs() -> List[Path]:
    candidates = [Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]
    result: List[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in result:
            result.append(resolved)
    return result


def prepare_sandbox_launch(
    argv: Sequence[str],
    *,
    cwd: Optional[os.PathLike[str] | str] = None,
    workspace: Optional[os.PathLike[str] | str] = None,
    mode: Optional[SandboxMode] = None,
    allow_network: Optional[bool] = None,
    backend: Optional[SandboxBackend] = None,
) -> SandboxLaunch:
    """Prepare argv according to config or explicit settings.

    ``off`` returns the original argv and makes no confinement claim.
    ``best_effort`` does the same only after warning when no backend is usable.
    ``required`` raises :class:`SandboxUnavailableError` instead of spawning.
    """
    if not argv:
        raise ValueError("Cannot sandbox an empty argv")

    configured_mode, configured_network, configured_workspace = _configured_settings()
    effective_mode = mode if mode is not None else configured_mode
    effective_network = allow_network if allow_network is not None else configured_network
    root = Path(workspace).expanduser().resolve() if workspace is not None else configured_workspace
    working_dir = Path(cwd).expanduser().resolve() if cwd is not None else root

    if effective_mode == "off":
        return SandboxLaunch(list(argv), sandboxed=False)
    if effective_mode not in ("best_effort", "required"):
        raise ValueError(f"Unknown sandbox mode: {effective_mode!r}")
    if not root.is_dir():
        reason = f"sandbox workspace is not a directory: {root}"
        if effective_mode == "required":
            raise SandboxUnavailableError(f"OS sandbox required but unavailable: {reason}")
        logger.warning("OS sandbox best_effort fallback: %s; running unconfined", reason)
        return SandboxLaunch(list(argv), False, fallback_reason=reason)

    selected = backend if backend is not None and backend.available() else None
    if selected is None and backend is None:
        selected = select_backend()
    if selected is None:
        reason = "no supported OS sandbox backend is available"
        if effective_mode == "required":
            raise SandboxUnavailableError(f"OS sandbox required but unavailable: {reason}")
        logger.warning("OS sandbox best_effort fallback: %s; running unconfined", reason)
        return SandboxLaunch(list(argv), False, fallback_reason=reason)

    wrapped = selected.wrap(
        argv,
        workspace=root,
        cwd=working_dir,
        allow_network=effective_network,
        temp_dirs=_writable_temp_dirs(),
    )
    return SandboxLaunch(wrapped, sandboxed=True, backend=selected.name)
