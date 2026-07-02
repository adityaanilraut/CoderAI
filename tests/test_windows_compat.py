"""Windows-compatibility regression tests.

These lock in the cross-platform fixes so a future change can't reintroduce
the POSIX-only calls that crashed CoderAI on Windows:

* ``os.fchmod`` is absent on Windows — :mod:`coderAI.system.fsperms` must
  degrade to a no-op instead of raising ``AttributeError`` mid-save.
* ``signal.SIGKILL`` does not exist on Windows — ``kill_process`` must not
  depend on it.
* The ``grep`` binary is absent on stock Windows — ``GrepTool`` must fall
  back to a pure-Python scan.
"""

import asyncio
import os
import signal
import tempfile
import time

import pytest

from coderAI.system import fsperms
from coderAI.tools.search import GrepTool
from coderAI.tools.terminal import (
    BgProcessInfo,
    KillProcessTool,
    RunBackgroundTool,
    _tracked_bg_processes,
)


# ---------------------------------------------------------------------------
# fsperms: owner-restriction helpers must never raise, even without os.fchmod
# ---------------------------------------------------------------------------


def test_restrict_fd_applies_on_posix():
    fd, path = tempfile.mkstemp()
    try:
        fsperms.restrict_fd(fd)
        if os.name != "nt":
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600
    finally:
        os.close(fd)
        os.unlink(path)


def test_restrict_fd_is_noop_when_fchmod_absent(monkeypatch):
    """Simulate Windows, where ``os.fchmod`` does not exist."""
    monkeypatch.delattr(os, "fchmod", raising=False)
    fd, path = tempfile.mkstemp()
    try:
        # Must not raise AttributeError.
        fsperms.restrict_fd(fd)
    finally:
        os.close(fd)
        os.unlink(path)


def test_restrict_fd_swallows_oserror(monkeypatch):
    def boom(_fd, _mode):
        raise OSError("not supported")

    monkeypatch.setattr(os, "fchmod", boom, raising=False)
    fd, path = tempfile.mkstemp()
    try:
        fsperms.restrict_fd(fd)  # best-effort: no raise
    finally:
        os.close(fd)
        os.unlink(path)


def test_restrict_path_noop_on_windows(monkeypatch, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x")
    called = {"n": 0}

    def spy(*_a, **_k):
        called["n"] += 1

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "chmod", spy)
    fsperms.restrict_path(target)
    assert called["n"] == 0  # short-circuits before touching os.chmod


# ---------------------------------------------------------------------------
# Config / history saves must work without os.fchmod (the Windows crash path)
# ---------------------------------------------------------------------------


def test_config_save_without_fchmod(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from coderAI.system.config import ConfigManager

    mgr = ConfigManager()
    mgr.config_dir = tmp_path / ".coderAI"
    mgr.config_dir.mkdir(parents=True, exist_ok=True)
    mgr.config_file = mgr.config_dir / "config.json"
    cfg = mgr.load()
    mgr.save(cfg)  # must not raise
    assert mgr.config_file.exists()


def test_history_save_without_fchmod(monkeypatch, tmp_path):
    monkeypatch.delattr(os, "fchmod", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from coderAI.system.history import HistoryManager

    mgr = HistoryManager()
    session = mgr.create_session(model="claude-4-sonnet")
    mgr.save_session(session)  # must not raise
    assert (mgr.history_dir / f"{session.session_id}.json").exists()


# ---------------------------------------------------------------------------
# KillProcessTool signals the whole process group, not just the leader
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.terminated = False
        self.pid = 4242

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        return self.returncode


def _capture_group_kill(monkeypatch):
    """Record the signal KillProcessTool passes to ``kill_process_group``."""
    calls = []

    def _spy(process, sig=signal.SIGKILL):
        calls.append(sig)
        # Simulate the group dying so ``process.wait()`` resolves promptly.
        process.returncode = -int(sig)

    monkeypatch.setattr("coderAI.tools.terminal.kill_process_group", _spy)
    return calls


def test_kill_process_force_signals_group_with_sigkill(monkeypatch):
    proc = _FakeProc()
    info = BgProcessInfo(proc, "sleep 999")  # type: ignore[arg-type]
    _tracked_bg_processes[proc.pid] = info
    calls = _capture_group_kill(monkeypatch)
    try:
        result = asyncio.run(KillProcessTool().execute(pid=proc.pid, force=True))
        assert result["success"]
        # The whole group is signalled (via the helper), with SIGKILL for force.
        assert calls == [signal.SIGKILL]
    finally:
        _tracked_bg_processes.pop(proc.pid, None)


def test_kill_process_graceful_signals_group_with_sigterm(monkeypatch):
    proc = _FakeProc()
    info = BgProcessInfo(proc, "sleep 999")  # type: ignore[arg-type]
    _tracked_bg_processes[proc.pid] = info
    calls = _capture_group_kill(monkeypatch)
    try:
        result = asyncio.run(KillProcessTool().execute(pid=proc.pid, force=False))
        assert result["success"]
        assert calls == [signal.SIGTERM]
    finally:
        _tracked_bg_processes.pop(proc.pid, None)


@pytest.mark.skipif(os.name == "nt", reason="process groups / os.kill(0) are POSIX")
def test_kill_process_reaps_backgrounded_grandchild(tmp_path):
    """A grandchild backgrounded inside the job must die when the job is killed.

    ``run_background`` spawns its child in a new session/group, so signalling the
    group (not just the leader) is what tears down a ``bash -c 'sleep & wait'``
    style grandchild. Prior to the fix KillProcessTool signalled only the leader,
    orphaning the inner ``sleep``.
    """

    async def _run():
        pidfile = tmp_path / "grandchild.pid"
        # Leader shell backgrounds a sleep (the grandchild), records its PID,
        # then stays alive itself so the leader is what we kill.
        cmd = f"sleep 300 & echo $! > {pidfile}; sleep 300"
        started = await RunBackgroundTool().execute(command=cmd)
        assert started["success"], started
        leader_pid = started["pid"]

        # Wait for the shell to write the grandchild PID.
        deadline = time.monotonic() + 3.0
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pidfile.exists(), "grandchild PID was never recorded"
        grandchild_pid = int(pidfile.read_text().strip())

        # Sanity: grandchild is alive before we kill the job.
        os.kill(grandchild_pid, 0)

        killed = await KillProcessTool().execute(pid=leader_pid, force=True)
        assert killed["success"], killed

        # The grandchild must be reaped along with the leader's group.
        reaped = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                reaped = True
                break
            await asyncio.sleep(0.05)
        # Best-effort cleanup if the assertion is about to fail.
        if not reaped:
            try:
                os.kill(grandchild_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert reaped, f"grandchild {grandchild_pid} was orphaned"

    try:
        asyncio.run(_run())
    finally:
        _tracked_bg_processes.clear()


# ---------------------------------------------------------------------------
# GrepTool must work when the grep binary is unavailable (Windows)
# ---------------------------------------------------------------------------


@pytest.fixture
def grep_tree(tmp_path):
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("def foo():\n    pass\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("hello from subdir\n")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "ignored.js").write_text("hello should be skipped\n")
    return tmp_path


def _force_python_grep(monkeypatch):
    """Make GrepTool believe the grep binary is missing (Windows-like)."""
    monkeypatch.setattr("coderAI.tools.search.shutil.which", lambda _name: None)


def test_grep_falls_back_to_python_when_binary_absent(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(GrepTool().execute(pattern="hello", path=str(grep_tree)))
    assert result["success"]
    assert result["count"] >= 2  # a.py and sub/c.txt
    assert all(isinstance(m["line"], int) for m in result["matches"])


def test_grep_python_fallback_skips_noise_dirs(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(GrepTool().execute(pattern="hello", path=str(grep_tree)))
    files = {m["file"] for m in result["matches"]}
    assert not any("node_modules" in f for f in files)


def test_grep_python_fallback_root_under_skip_named_dir(monkeypatch, tmp_path):
    # The search root itself lives under a skip-named ancestor (a realistic CI
    # checkout at e.g. ``C:\build\proj``). Filtering on the *absolute* path would
    # skip every file and silently return zero matches; the skip filter must be
    # applied relative to the search base instead.
    _force_python_grep(monkeypatch)
    proj = tmp_path / "build" / "proj"
    proj.mkdir(parents=True)
    (proj / "file.txt").write_text("needle here\n")

    result = asyncio.run(GrepTool().execute(pattern="needle", path=str(proj)))
    assert result["success"]
    assert result["count"] == 1  # was 0 before the fix

    # A genuine skip dir *below* the base is still skipped.
    nm = proj / "node_modules"
    nm.mkdir()
    (nm / "x.js").write_text("needle in vendored code\n")
    result2 = asyncio.run(GrepTool().execute(pattern="needle", path=str(proj)))
    assert result2["count"] == 1
    assert not any("node_modules" in m["file"] for m in result2["matches"])


def test_grep_python_fallback_case_insensitive(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(
        GrepTool().execute(pattern="HELLO", path=str(grep_tree), case_insensitive=True)
    )
    assert result["success"]
    assert result["count"] >= 2


def test_grep_python_fallback_single_file(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(GrepTool().execute(pattern="hello", path=str(grep_tree / "a.py")))
    assert result["success"]
    assert result["count"] == 1


def test_grep_python_fallback_max_results(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(
        GrepTool().execute(pattern="def", path=str(grep_tree), max_results=1)
    )
    assert result["success"]
    assert result["count"] == 1
    assert result["was_truncated"] is True


def test_grep_python_fallback_invalid_regex(monkeypatch, grep_tree):
    _force_python_grep(monkeypatch)
    result = asyncio.run(GrepTool().execute(pattern="[unclosed", path=str(grep_tree)))
    assert result["success"] is False
    assert "regex" in result["error"].lower()
