"""Phase 1.3 — package_manager must not fetch/run arbitrary code.

The validator used to only reject shell metacharacters, so ``git+https://…``,
``./local`` and ``https://…`` sailed through and let pip run an attacker's
``setup.py``; a package token beginning with ``-`` was parsed as a pip flag
(``--index-url=…``). These tests pin the remote-source / flag-injection
rejections and confirm a ``--`` separator precedes the package token.
"""

from __future__ import annotations

import asyncio

import pytest

from coderAI.system import proc
from coderAI.tools import package_manager as pm
from coderAI.tools.package_manager import PackageManagerTool, _validate_package_name

# ── Rejected package specs (remote/VCS/local/flag) ───────────────────────────
REJECTED = [
    "git+https://evil.example/x.git",
    "git+ssh://evil.example/x.git",
    "hg+https://evil.example/x",
    "svn+https://evil.example/x",
    "https://evil.example/x.tar.gz",
    "http://evil.example/x",
    "file:///etc/passwd",
    "./local-evil",
    "../escape",
    "/abs/evil",
    "~/in-home",
    "-e",
    "--index-url=http://evil.example/simple",
    "-rrequirements.txt",
]


@pytest.mark.parametrize("spec", REJECTED)
def test_validator_rejects_dangerous_sources(spec: str) -> None:
    assert _validate_package_name(spec, "pip") is not None, f"should reject {spec!r}"


# ── Accepted registry names ──────────────────────────────────────────────────
ACCEPTED = [
    ("requests", "pip"),
    ("requests==2.31.0", "pip"),
    ("Django", "pip"),
    ("lodash", "npm"),
    ("test-pkg", "npm"),
    ("@scope/package", "npm"),
    ("serde", "cargo"),
    ("github.com/user/repo", "go"),  # go module paths legitimately contain '/'
]


@pytest.mark.parametrize("spec,manager", ACCEPTED)
def test_validator_accepts_registry_names(spec: str, manager: str) -> None:
    assert _validate_package_name(spec, manager) is None, f"should accept {spec!r}"


def test_allow_remote_source_opt_in_permits_vcs() -> None:
    spec = "git+https://example.com/x.git"
    assert _validate_package_name(spec, "pip") is not None
    assert _validate_package_name(spec, "pip", allow_remote_source=True) is None


# ── argv construction: `--` precedes the package token ───────────────────────


def _patch_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch shutil.which + create_subprocess_exec, capturing the argv.

    The spawn now happens inside ``system.proc.run_scrubbed`` (which scrubs the
    env before delegating to ``create_subprocess_exec``), so patch it there.
    """
    captured: dict = {}

    monkeypatch.setattr(pm.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):  # noqa: A002 - matches asyncio API
            return (b"", b"")

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def _fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(proc.asyncio, "create_subprocess_exec", _fake_exec)
    return captured


def test_pip_install_inserts_double_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_capture(monkeypatch)
    result = asyncio.run(
        PackageManagerTool().execute(action="install", package="requests", manager="pip")
    )
    assert result["success"] is True, result
    cmd = captured["cmd"]
    assert "--" in cmd, cmd
    # The package token comes immediately after the `--` end-of-options marker.
    assert cmd[cmd.index("--") + 1] == "requests"
    # And nothing after `--` is parsed as a flag.
    assert cmd[-1] == "requests"


def test_pip_install_with_version_after_double_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_capture(monkeypatch)
    result = asyncio.run(
        PackageManagerTool().execute(
            action="install", package="requests", version="==2.31.0", manager="pip"
        )
    )
    assert result["success"] is True, result
    cmd = captured["cmd"]
    assert cmd[cmd.index("--") + 1] == "requests==2.31.0"
    # No accidental double-append of the bare package name.
    assert cmd.count("requests") == 0
    assert cmd.count("requests==2.31.0") == 1


def test_install_flag_injection_rejected_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch)
    result = asyncio.run(
        PackageManagerTool().execute(
            action="install", package="--index-url=http://evil.example", manager="pip"
        )
    )
    assert result["success"] is False
    assert "flag" in result["error"].lower() or "-" in result["error"]
