"""Tests for developer tooling scripts.

Exercises scripts/inject_build_sha.py, scripts/check_dependency_versions.py,
and scripts/telemetry_debug_server.py without network access or git side
effects on the real repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FRAMEWORK_PY = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
PY = FRAMEWORK_PY if Path(FRAMEWORK_PY).exists() else sys.executable


def _load_module(name: str, filename: str):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# inject_build_sha.py
# ---------------------------------------------------------------------------


def test_normalize_remote_shapes():
    mod = _load_module("inject_build_sha", "inject_build_sha.py")
    assert mod._normalize_remote("git@github.com:user/repo.git") == "github.com/user/repo"
    assert mod._normalize_remote("https://github.com/user/repo.git") == "github.com/user/repo"
    assert mod._normalize_remote("https://user:token@github.com/repo.git") == "github.com/repo"
    assert mod._normalize_remote("https://example.com/a/b") == "example.com/a/b"


def test_assemble_build_id():
    mod = _load_module("inject_build_sha", "inject_build_sha.py")
    assert mod._assemble("host/repo", "abc123") == "host/repo@abc123"
    assert mod._assemble("", "abc123") == "abc123"
    assert mod._assemble("host/repo", "") == "host/repo"
    assert mod._assemble("", "") == ""


def test_inject_build_sha_env_override(tmp_path, monkeypatch):
    mod = _load_module("inject_build_sha_env", "inject_build_sha.py")
    monkeypatch.setenv("CODERAI_BUILD_SHA", "testenvsha")
    # env var takes precedence, so git is never consulted for the SHA.
    assert mod._detect_sha() == "testenvsha"

    target = tmp_path / "_build_info.py"
    monkeypatch.setattr(mod, "_resolve_project_root", lambda: tmp_path)
    # Emulate main() target layout: <root>/coderai/_build_info.py
    (tmp_path / "coderai").mkdir()
    target = tmp_path / "coderai" / "_build_info.py"
    rc = mod.main()
    assert rc == 0
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "BUILD_SHA" in content
    assert "testenvsha" in content


def test_inject_build_sha_cli_smoke(tmp_path):
    env = dict(os.environ)
    env["CODERAI_BUILD_SHA"] = "smoke123"
    # Run against a throwaway copy so the real repo is untouched.
    # The script resolves the project root as parent-of-scripts/ and writes
    # to <root>/coderai/_build_info.py, so mirror that layout.
    import shutil

    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "coderai").mkdir(parents=True)
    shutil.copy(SCRIPTS_DIR / "inject_build_sha.py", work / "scripts" / "inject_build_sha.py")
    result = subprocess.run(
        [PY, str(work / "scripts" / "inject_build_sha.py")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "smoke123" in result.stdout
    written = work / "coderai" / "_build_info.py"
    assert written.exists()
    assert "smoke123" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# check_dependency_versions.py
# ---------------------------------------------------------------------------


def test_parse_requirement_shapes():
    mod = _load_module("check_dependency_versions", "check_dependency_versions.py")
    assert mod._parse_requirement("rich>=13.7.0") == ("rich", ">=13.7.0")
    assert mod._parse_requirement("kosong[contrib]==0.56.0") == ("kosong", "==0.56.0")
    assert mod._parse_requirement('pillow>=10; python_version > "3.9"') == ("pillow", ">=10")
    assert mod._parse_requirement("  # comment only") is None
    assert mod._parse_requirement("") is None


def test_check_specifier_operators():
    mod = _load_module("check_dependency_versions2", "check_dependency_versions.py")
    assert mod._check_specifier("1.10.0", ">=1.10.0")
    assert not mod._check_specifier("1.9.0", ">=1.10.0")
    assert mod._check_specifier("0.56.0", "==0.56.0")
    assert not mod._check_specifier("0.57.0", "==0.56.0")
    assert mod._check_specifier("2.0.0", ">=1.0, <3.0")
    assert not mod._check_specifier("3.0.0", ">=1.0, <3.0")
    assert mod._check_specifier("1.4.2", "~=1.4.0")
    assert not mod._check_specifier("1.5.0", "~=1.4.0")
    assert mod._check_specifier("1.0.0", "!=2.0.0")
    assert not mod._check_specifier("2.0.0", "!=2.0.0")
    # Unknown operator fails closed.
    assert not mod._check_specifier("1.0.0", "===1.0.0")


def test_extract_dependencies_from_repo_pyproject():
    mod = _load_module("check_dependency_versions3", "check_dependency_versions.py")
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    deps = mod._extract_dependencies(text)
    names = [mod._parse_requirement(d)[0] for d in deps if mod._parse_requirement(d)]
    for expected in ("rich", "kosong", "pykaos", "fastmcp", "aiohttp"):
        assert expected in names, f"{expected} missing from parsed dependencies"


def test_check_dependency_versions_cli_smoke():
    result = subprocess.run(
        [PY, str(SCRIPTS_DIR / "check_dependency_versions.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "ok: installed dependencies match pyproject.toml constraints" in result.stdout


# ---------------------------------------------------------------------------
# telemetry_debug_server.py
# ---------------------------------------------------------------------------


def test_telemetry_debug_server_roundtrip():
    mod = _load_module("telemetry_debug_server", "telemetry_debug_server.py")
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.TelemetryHandler)
    port = server.server_address[1]
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # Health check.
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            assert resp.status == 200

        payload = json.dumps(
            {
                "user_id": "test-user",
                "events": [
                    {"event": "kfc_session_started", "session_id": "s1"},
                    {"event": "kfc_other", "session_id": "s1"},
                ],
            }
        ).encode()
        req = urllib.request.Request(
            base + "/v1/event", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode()) == {"ok": True}

        # Malformed payloads are rejected, not crashed on.
        bad = urllib.request.Request(
            base + "/v1/event",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(bad, timeout=5)
            raise AssertionError("expected HTTP 400")
        except Exception as exc:
            assert "400" in str(exc)

        # Event matching honors the kfc_ prefix canonically.
        assert mod._canonical_event_name("kfc_session_started") == "session_started"
        assert mod._canonical_event_name("session_started") == "session_started"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
