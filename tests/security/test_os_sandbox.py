"""OS-level execution sandbox selection, wrapping, and real confinement."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from coderAI.core.services import services_scope
from coderAI.system.config import Config
from coderAI.system.proc import command_argv, run_scrubbed
from coderAI.system.sandbox import (
    BubblewrapBackend,
    SandboxBackend,
    SandboxExecBackend,
    SandboxUnavailableError,
    prepare_sandbox_launch,
    select_backend,
)


class _FakeBackend(SandboxBackend):
    name = "fake"

    def __init__(self, usable: bool) -> None:
        self.usable = usable

    def available(self) -> bool:
        return self.usable

    def wrap(self, argv, *, workspace, cwd, allow_network, temp_dirs):
        del workspace, cwd, allow_network, temp_dirs
        return ["fake-sandbox", "--", *argv]


def test_select_backend_uses_first_available() -> None:
    unavailable = _FakeBackend(False)
    available = _FakeBackend(True)
    assert select_backend([unavailable, available]) is available


def test_bubblewrap_wrapper_is_argv_and_denies_network_by_default(tmp_path: Path) -> None:
    backend = BubblewrapBackend("/usr/bin/bwrap")
    wrapped = backend.wrap(
        ["sh", "-c", "printf '%s' 'a b'"],
        workspace=tmp_path,
        cwd=tmp_path,
        allow_network=False,
        temp_dirs=[],
    )
    assert wrapped[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in wrapped
    assert "--share-net" not in wrapped
    assert wrapped[-3:] == ["sh", "-c", "printf '%s' 'a b'"]
    assert ["--bind", str(tmp_path), str(tmp_path)] == wrapped[-9:-6]


def test_sandbox_exec_wrapper_contains_write_and_network_policy(tmp_path: Path) -> None:
    backend = SandboxExecBackend("/usr/bin/sandbox-exec")
    wrapped = backend.wrap(
        ["python3", "script with spaces.py"],
        workspace=tmp_path,
        cwd=tmp_path,
        allow_network=False,
        temp_dirs=[],
    )
    assert wrapped[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny file-write*)" in wrapped[2]
    assert "(deny network*)" in wrapped[2]
    assert 'literal "/dev/null"' in wrapped[2]
    assert str(tmp_path) in wrapped[2]
    assert wrapped[-2:] == ["python3", "script with spaces.py"]


def test_network_opt_in_changes_backend_policies(tmp_path: Path) -> None:
    bwrap = BubblewrapBackend("/usr/bin/bwrap").wrap(
        ["true"],
        workspace=tmp_path,
        cwd=tmp_path,
        allow_network=True,
        temp_dirs=[],
    )
    macos = SandboxExecBackend("/usr/bin/sandbox-exec").wrap(
        ["true"],
        workspace=tmp_path,
        cwd=tmp_path,
        allow_network=True,
        temp_dirs=[],
    )
    assert "--share-net" in bwrap
    assert "(deny network*)" not in macos[2]


def test_shell_command_is_one_argv_element() -> None:
    command = "printf '%s' \"$value with spaces\""
    argv = command_argv(command, shell=True)
    assert argv[-2:] == ["-c", command] or argv[-1] == command


def test_required_mode_fails_closed_when_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("coderAI.system.sandbox.select_backend", lambda: None)
    with pytest.raises(SandboxUnavailableError, match="required.*unavailable"):
        prepare_sandbox_launch(["true"], workspace=tmp_path, mode="required")


def test_best_effort_fallback_is_explicit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr("coderAI.system.sandbox.select_backend", lambda: None)
    with caplog.at_level(logging.WARNING):
        launch = prepare_sandbox_launch(["true"], workspace=tmp_path, mode="best_effort")
    assert launch.sandboxed is False
    assert launch.fallback_reason
    assert "running unconfined" in caplog.text


def _real_backend_or_skip() -> SandboxBackend:
    backend = select_backend()
    if backend is None:
        pytest.skip("No usable Bubblewrap or sandbox-exec backend on this host")
    return backend


def test_real_sandbox_allows_workspace_write_and_denies_outside(tmp_path: Path) -> None:
    _real_backend_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = Path.home() / f".coderai-sandbox-test-{uuid4().hex}"
    if outside.parent.resolve() == tmp_path.resolve() or not outside.parent.is_dir():
        pytest.skip("No deterministic writable path outside workspace and temp directories")

    code = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('inside'); "
        "Path(sys.argv[2]).write_text('outside')"
    )
    try:
        with services_scope(config=Config(project_root=str(workspace), sandbox_mode="required")):
            returncode, _, stderr, timed_out = asyncio.run(
                run_scrubbed(
                    [sys.executable, "-c", code, str(workspace / "inside.txt"), str(outside)],
                    cwd=workspace,
                    timeout=20,
                )
            )
        assert timed_out is False
        assert returncode != 0, stderr.decode(errors="replace")
        assert (workspace / "inside.txt").read_text() == "inside"
        assert not outside.exists(), "sandbox allowed a write outside workspace/temp"
    finally:
        outside.unlink(missing_ok=True)


@pytest.mark.enable_socket
def test_real_sandbox_denies_network_by_default(tmp_path: Path) -> None:
    _real_backend_or_skip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    code = (
        "import socket, sys; s=socket.socket(); "
        "s.settimeout(2); s.connect(('127.0.0.1', int(sys.argv[1])))"
    )
    try:
        with services_scope(config=Config(project_root=str(workspace), sandbox_mode="required")):
            returncode, _, _, timed_out = asyncio.run(
                run_scrubbed(
                    [sys.executable, "-c", code, str(port)],
                    cwd=workspace,
                    timeout=10,
                )
            )
        assert timed_out is False
        assert returncode != 0, "sandboxed child connected to the local network"
    finally:
        listener.close()
