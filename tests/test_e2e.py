"""Offline-only in-process e2e: info, validation, version, export.

Every test runs `main`/`_build_parser` in-process (plus one sanitized
subprocess) with HOME redirected to tmp and credential env stripped, so no
network, keys, or LLM are ever touched.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import zipfile

import pytest


def _isolate_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect HOME to tmp and strip credential/proxy env vars."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    for var in list(os.environ):
        if var.startswith(("CODERAI_", "OPENAI_", "ANTHROPIC_", "GEMINI_")):
            monkeypatch.delenv(var, raising=False)


def test_e2e_info_human_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`info` prints human-readable version lines and exits zero."""
    from coderai.ui.shell.app import main

    _isolate_env(tmp_path, monkeypatch)
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "coderai version" in out


def test_e2e_info_json_returns_parseable_json(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`info --json` prints a parseable payload with a version key."""
    from coderai.ui.shell.app import main

    _isolate_env(tmp_path, monkeypatch)
    assert main(["info", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coderai_version"]


def test_e2e_validation_rejects_positional_plus_prompt_flag(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A positional prompt combined with --prompt is rejected non-zero."""
    from coderai.ui.shell.app import main

    _isolate_env(tmp_path, monkeypatch)
    assert main(["hello", "--prompt", "world"]) == 1


def test_e2e_version_flag_exits_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--version exits via SystemExit(0) like a standard CLI flag."""
    from coderai.ui.shell.app import main

    _isolate_env(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_e2e_export_creates_zip_in_tmp(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export <id> -o <zip> -y` zips session files with a manifest."""
    from coderai.ui.shell.app import main

    _isolate_env(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    session_id = "e2e-session-abcdef123456"
    store_dir = tmp_path / ".coderai" / "sessions"
    store_dir.mkdir(parents=True)
    (store_dir / f"{session_id}.jsonl").write_text(
        json.dumps({"timestamp": 1700000000000, "role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )
    (store_dir / "sessions-index.json").write_text(
        json.dumps({"entries": [{"id": session_id}]}), encoding="utf-8"
    )
    out_zip = tmp_path / "out" / "session.zip"
    assert main(["export", session_id, "-o", str(out_zip), "-y"]) == 0
    assert out_zip.is_file()
    with zipfile.ZipFile(out_zip) as archive:
        assert "manifest.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["session_id"] == session_id


def test_e2e_info_json_subprocess_matches_in_process(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -m coderai info --json` in a sanitized env returns JSON."""
    _isolate_env(tmp_path, monkeypatch)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODERAI_", "OPENAI_", "ANTHROPIC_", "GEMINI_"))
    }
    env["HOME"] = str(tmp_path / "home")
    env["NO_COLOR"] = "1"
    # Fake HOME hides user site-packages (rich/pygments live there), so put
    # the repo root and the real user site back on the import path only.
    import site

    repo_root = str(pathlib.Path(__file__).resolve().parent.parent)
    user_site = site.getusersitepackages()
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([repo_root, user_site] + ([prior] if prior else []))
    proc = subprocess.run(
        [sys.executable, "-m", "coderai", "info", "--json"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["coderai_version"]
