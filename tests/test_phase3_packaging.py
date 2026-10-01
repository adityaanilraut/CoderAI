"""Portable installed search, selected interpreter, and source-stamp regressions."""

from __future__ import annotations

import errno
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from coderai.tools.file import _search_common as common
from coderai.tools.file.glob import handle_glob_tool
from coderai.tools.file.grep import handle_grep_tool
from coderai.tools.legacy.types import ToolExecutionContext

ROOT = Path(__file__).resolve().parents[1]


def _script_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "platform,machine",
    [
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "aarch64"),
        ("linux", "x86_64"),
        ("win32", "AMD64"),
        ("win32", "ARM64"),
    ],
)
def test_portable_search_without_native_binary(tmp_path, monkeypatch, platform, machine):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(common.shutil, "which", lambda _: None)
    monkeypatch.delenv("CODERAI_RG_PATH", raising=False)
    monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)
    (tmp_path / "sample.py").write_text("before\nPortable needle\nafter\n")
    (tmp_path / "other.txt").write_text("needle\n")
    ctx = ToolExecutionContext(session_id=f"{platform}-{machine}", project_root=str(tmp_path))
    assert common.resolve_rg_path() is None
    grep = handle_grep_tool({"pattern": "portable", "ignore_case": True, "include": "*.py"}, ctx)
    assert grep.ok and grep.metadata["matches"] == [
        {"path": "sample.py", "lineNumber": 2, "line": "Portable needle"}
    ]
    glob = handle_glob_tool({"pattern": "**/*.py"}, ctx)
    assert glob.ok and glob.metadata["paths"] == ["sample.py"]


def test_incompatible_explicit_rg_selects_valid_path(tmp_path, monkeypatch):
    incompatible = tmp_path / "bad-rg"
    valid = tmp_path / "rg"
    for path in (incompatible, valid):
        path.write_text("fake")
        path.chmod(0o755)
    monkeypatch.setenv("CODERAI_RG_PATH", str(incompatible))
    monkeypatch.setattr(common.shutil, "which", lambda _: str(valid))

    def run(argv, **kwargs):
        if argv[0] == str(incompatible):
            raise OSError(errno.ENOEXEC, "incompatible architecture")
        return subprocess.CompletedProcess(argv, 0, stdout=b"ripgrep 14.1.0\n", stderr=b"")

    monkeypatch.setattr(common.subprocess, "run", run)
    assert common.resolve_rg_path() == str(valid)


@pytest.mark.parametrize(
    "tool,arguments",
    [(handle_glob_tool, {"pattern": "*.txt"}), (handle_grep_tool, {"pattern": "needle"})],
)
def test_launch_race_falls_back(tmp_path, monkeypatch, tool, arguments):
    module = sys.modules[tool.__module__]
    monkeypatch.setattr(module, "resolve_rg_path", lambda: "vanished-rg")
    monkeypatch.delenv("CODERAI_SEARCH_BACKEND", raising=False)

    def unavailable(*args, **kwargs):
        raise common.SearchError("executable vanished after resolution", "SEARCH_UNAVAILABLE")

    monkeypatch.setattr(module, "run_ripgrep", unavailable)
    (tmp_path / "one.txt").write_text("needle\n")
    result = tool(arguments, ToolExecutionContext(session_id="race", project_root=str(tmp_path)))
    assert result.ok and result.metadata["count"] == 1


@pytest.mark.parametrize("version", ["3.10", "3.11", "3.12", "3.14"])
@pytest.mark.parametrize("installer", ["uv", "pipx", "pip"])
def test_posix_installer_uses_selected_supported_interpreter(tmp_path, version, installer):
    commands = tmp_path / "commands with spaces"
    commands.mkdir()
    selected = commands / "chosen python"
    log = tmp_path / "calls"
    selected.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-c" ]]; then\n'
        '  case "$2" in\n'
        '    *sys.executable*) printf "%s\\n" "$0" ;;\n'
        f'    *version_info.major*) echo "{version}" ;;\n'
        f'    *version_info*) echo "{int(tuple(map(int, version.split("."))) >= (3, 12))}" ;;\n'
        "  esac\n"
        'else printf "%s\\n" "$@" >> "$CALL_LOG"; fi\n'
    )
    selected.chmod(0o755)
    if installer != "pip":
        command = commands / installer
        command.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >> "$CALL_LOG"\n')
        command.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(commands),
        "CODERAI_PYTHON": str(selected),
        "CALL_LOG": str(log),
    }
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/install.sh")], env=env, capture_output=True, text=True
    )
    if version in ("3.10", "3.11"):
        assert result.returncode == 1 and "3.12" in result.stderr
        assert not log.exists()
    else:
        assert result.returncode == 0, result.stderr
        args = log.read_text().splitlines()
        if installer == "pip":
            assert args == ["-m", "pip", "install", "--user", "coderai-agent"]
        else:
            assert args == (["tool", "install"] if installer == "uv" else ["install"]) + [
                "--python",
                str(selected),
                "coderai-agent",
            ]


def test_git_stamp_retains_full_commit(monkeypatch):
    script = _script_module("inject_build_sha")
    sha = "ab" * 20
    monkeypatch.delenv("CODERAI_BUILD_SHA", raising=False)
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=sha + "\n"),
    )
    assert script._detect_sha() == sha


def test_stamp_verifier_rejects_short_or_wrong_sha():
    verifier = _script_module("verify_wheel")
    sha = "ab" * 20
    assert (
        verifier._verify_stamp(f'BUILD_SHA = "repo@{sha}"\n'.encode(), sha, label="test")
        == f"repo@{sha}"
    )
    for wrong in (sha[:12], "cd" * 20, ""):
        with pytest.raises(SystemExit, match="does not match"):
            verifier._verify_stamp(f'BUILD_SHA = "{wrong}"\n'.encode(), sha, label="test")


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "member",
    ["coderai/tools/__pycache__/file.pyc", "coderai/.DS_Store", "coderai/.ruff_cache/check"],
)
def test_release_archives_reject_generated_caches(tmp_path, kind, member):
    import io
    import tarfile
    import zipfile

    verifier = _script_module("verify_wheel")
    if kind == "wheel":
        artifact = tmp_path / "test.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(member, b"generated")
        with pytest.raises(SystemExit, match="generated caches"):
            verifier._verify_wheel_archive(artifact, expected_version="0.5.0")
    else:
        artifact = tmp_path / "test.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            info = tarfile.TarInfo("test-0.5.0/" + member)
            info.size = 9
            archive.addfile(info, io.BytesIO(b"generated"))
        with pytest.raises(SystemExit, match="generated caches"):
            verifier._verify_sdist(artifact, wheel_version="0.5.0")


def test_python_grep_multiline_and_type(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_SEARCH_BACKEND", "python")
    (tmp_path / "one.py").write_text("start\nfinish\n")
    (tmp_path / "two.txt").write_text("start\nfinish\n")
    ctx = ToolExecutionContext(session_id="options", project_root=str(tmp_path))
    result = handle_grep_tool({"pattern": "start\nfinish", "multiline": True, "type": "py"}, ctx)
    assert result.ok and result.metadata["matches"] == [
        {"path": "one.py", "lineNumber": 1, "line": "start"},
        {"path": "one.py", "lineNumber": 2, "line": "finish"},
    ]
    unsupported = handle_grep_tool({"pattern": "start", "type": "unknown-language"}, ctx)
    assert not unsupported.ok and unsupported.metadata["code"] == "SEARCH_UNSUPPORTED_OPTION"
