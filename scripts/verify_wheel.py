#!/usr/bin/env python3
"""Verify release archives and probe an installed wheel outside the checkout."""

from __future__ import annotations

import argparse
import email
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import venv
import zipfile

DIST_NAME = "coderai-agent"
ENTRY_POINTS = {
    "coderai = coderai.main:main",
}

RUNTIME_MEMBERS = (
    "coderai/__init__.py",
    "coderai/__main__.py",
    "coderai/_version.py",
    "coderai/main.py",
    "coderai/py.typed",
    "coderai/cli/__init__.py",
    "coderai/cli/app.py",
    "coderai/cli/ascii_art.py",
    "coderai/cli/diff_render.py",
    "coderai/cli/exit_summary.py",
    "coderai/cli/file_mention.py",
    "coderai/cli/interactive_menu.py",
    "coderai/cli/plan_render.py",
    "coderai/cli/status_bar.py",
    "coderai/cli/thinking.py",
    "coderai/cli/tool_card.py",
    "coderai/cli/welcome.py",
    "coderai/core/__init__.py",
    "coderai/core/session.py",
    "coderai/core/prompt.py",
    "coderai/core/permissions.py",
    "coderai/core/settings.py",
    "coderai/core/state.py",
    "coderai/core/openai_client.py",
    "coderai/core/subagent.py",
    "coderai/core/common/__init__.py",
    "coderai/core/common/file_history.py",
    "coderai/core/common/message_converter.py",
    "coderai/core/common/file_utils.py",
    "coderai/core/common/shell_utils.py",
    "coderai/core/common/process_tree.py",
    "coderai/core/common/bash_timeout.py",
    "coderai/core/common/openai_thinking.py",
    "coderai/core/common/model_capabilities.py",
    "coderai/core/common/error_logger.py",
    "coderai/core/common/debug_logger.py",
    "coderai/core/common/llm_error.py",
    "coderai/core/common/validate.py",
    "coderai/core/mcp/__init__.py",
    "coderai/core/mcp/client.py",
    "coderai/core/mcp/manager.py",
    "coderai/core/tools/__init__.py",
    "coderai/core/tools/types.py",
    "coderai/core/tools/executor.py",
    "coderai/core/tools/read.py",
    "coderai/core/tools/edit.py",
    "coderai/core/tools/write.py",
    "coderai/core/tools/bash.py",
    "coderai/core/tools/ask_user_question.py",
    "coderai/core/tools/update_plan.py",
    "coderai/core/tools/web_search.py",
    "coderai/core/tools/understand_image.py",
    "coderai/core/tools/subagent.py",
)


def _resolve_archives(value: str) -> tuple[Path, Path | None]:
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        wheels = sorted(path.glob("*.whl"))
        sdists = sorted(path.glob("*.tar.gz"))
    else:
        wheels = [path] if path.suffix == ".whl" else []
        sdists = sorted(path.parent.glob("*.tar.gz")) if wheels else []
    if len(wheels) != 1 or not wheels[0].is_file():
        raise SystemExit(f"Expected exactly one wheel at {path}, found {len(wheels)}")
    if len(sdists) > 1:
        raise SystemExit(f"Expected at most one sdist beside {wheels[0]}, found {len(sdists)}")
    return wheels[0], sdists[0] if sdists else None


def _message(raw: bytes, *, label: str) -> email.message.Message:
    message = email.message_from_bytes(raw)
    if not message.get("Name") or not message.get("Version"):
        raise SystemExit(f"{label} metadata is missing Name or Version")
    return message


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _verify_metadata(
    message: email.message.Message,
    *,
    label: str,
    expected_version: str | None,
) -> str:
    name = str(message["Name"])
    version = str(message["Version"])
    if _normalized_name(name) != DIST_NAME:
        raise SystemExit(f"{label} distribution name is {name!r}, expected {DIST_NAME!r}")
    if expected_version is not None and version != expected_version:
        raise SystemExit(f"{label} version {version!r} does not match {expected_version!r}")
    if message.get("Requires-Python") != ">=3.10":
        raise SystemExit(f"{label} must declare Requires-Python: >=3.10")
    return version


def _verify_wheel_archive(wheel: Path, *, expected_version: str | None) -> str:
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        missing = sorted(set(RUNTIME_MEMBERS) - members)
        if missing:
            raise SystemExit("Wheel is missing runtime files: " + ", ".join(missing))
        metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in members if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise SystemExit("Wheel must contain exactly one METADATA and entry_points.txt")
        metadata = _message(archive.read(metadata_names[0]), label="Wheel")
        entry_points = {
            line.strip()
            for line in archive.read(entry_names[0]).decode("utf-8").splitlines()
            if line.strip()
        }
    for ep in ENTRY_POINTS:
        if ep not in entry_points:
            raise SystemExit(f"Wheel console entry point is missing: {ep}")
    return _verify_metadata(metadata, label="Wheel", expected_version=expected_version)


def _verify_sdist(sdist: Path, *, wheel_version: str) -> None:
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
        roots = {name.split("/", 1)[0] for name in members}
        if len(roots) != 1:
            raise SystemExit("Sdist must contain exactly one top-level directory")
        root = next(iter(roots))
        required = {
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "MANIFEST.in",
            "pyproject.toml",
            *RUNTIME_MEMBERS,
        }
        missing = sorted(f"{root}/{name}" for name in required if f"{root}/{name}" not in members)
        if missing:
            raise SystemExit("Sdist is missing release files: " + ", ".join(missing))
        pkg_info = archive.extractfile(f"{root}/PKG-INFO")
        if pkg_info is None:
            raise SystemExit("Sdist is missing PKG-INFO")
        metadata = _message(pkg_info.read(), label="Sdist")
    _verify_metadata(metadata, label="Sdist", expected_version=wheel_version)


def _venv_python(environment: Path) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return environment / directory / executable


def _venv_script(environment: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return environment / directory / f"{name}{suffix}"


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        rendered = " ".join(command)
        raise SystemExit(
            f"Installed wheel command failed: {rendered}\n"
            f"stdout:\n{error.stdout}\nstderr:\n{error.stderr}"
        ) from error


def _core_probe() -> str:
    return """
import importlib.metadata
import sys

from coderai._version import __version__
from coderai.core.prompt import get_system_prompt, get_tools
from coderai.core.session import SessionManager
from coderai.core.common.file_history import GitFileHistory
from coderai.core.tools.executor import ToolExecutor

metadata = importlib.metadata.metadata('coderai-agent')
assert __version__ == metadata['Version'], (__version__, metadata['Version'])
tools = get_tools()
tool_names = {t['function']['name'] for t in tools}
assert {'bash', 'read', 'write', 'edit', 'AskUserQuestion', 'UpdatePlan', 'WebSearch'} <= tool_names
print('core-probe-ok')
"""


def _verify_installed(
    wheel: Path,
    *,
    expected_version: str,
    install_dependencies: bool,
    system_site_packages: bool,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="coderai-wheel-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        empty_project = root / "empty-project"
        empty_home = root / "empty-home"
        empty_project.mkdir()
        empty_home.mkdir()
        venv.EnvBuilder(
            with_pip=True,
            clear=True,
            system_site_packages=system_site_packages,
        ).create(environment)
        python = _venv_python(environment)
        install = [os.fspath(python), "-m", "pip", "install", "--disable-pip-version-check"]
        if not install_dependencies:
            install.append("--no-deps")
        target = os.fspath(wheel)
        install.append(target)
        _run_checked(install, cwd=empty_project, env=os.environ.copy())

        child_environment = os.environ.copy()
        child_environment["HOME"] = os.fspath(empty_home)
        child_environment["USERPROFILE"] = os.fspath(empty_home)
        child_environment.pop("PYTHONPATH", None)
        child_environment["PYTHONNOUSERSITE"] = "1"

        executable = _venv_script(environment, "coderai")
        version_out = _run_checked(
            [os.fspath(executable), "--version"], cwd=empty_project, env=child_environment
        )
        assert expected_version in version_out.stdout or expected_version in version_out.stderr

        _run_checked([os.fspath(executable), "--help"], cwd=empty_project, env=child_environment)
        probe_res = _run_checked(
            [os.fspath(python), "-I", "-c", _core_probe()],
            cwd=empty_project,
            env=child_environment,
        )
        assert "core-probe-ok" in probe_res.stdout

        return {"installation": "ok", "probe": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="Wheel path, or directory containing one wheel and sdist")
    parser.add_argument(
        "--expected-version",
        help="Require wheel, sdist, and installed runtime to use this exact version",
    )
    parser.add_argument(
        "--no-deps",
        action="store_true",
        help="Install the wheel without dependencies (use with --system-site-packages locally)",
    )
    parser.add_argument(
        "--system-site-packages",
        action="store_true",
        help="Expose the invoking Python's site packages inside the smoke-test environment",
    )
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Check wheel and adjacent sdist without creating an installation environment",
    )
    args = parser.parse_args()

    wheel, sdist = _resolve_archives(args.wheel)
    wheel_version = _verify_wheel_archive(wheel, expected_version=args.expected_version)
    if sdist is not None:
        _verify_sdist(sdist, wheel_version=wheel_version)
    if args.archive_only:
        print(
            json.dumps(
                {
                    "wheel": wheel.name,
                    "sdist": sdist.name if sdist else None,
                    "version": wheel_version,
                    "archive": "ok",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    result = _verify_installed(
        wheel,
        expected_version=wheel_version,
        install_dependencies=not args.no_deps,
        system_site_packages=args.system_site_packages,
    )
    print(
        json.dumps(
            {
                "wheel": wheel.name,
                "sdist": sdist.name if sdist else None,
                **result,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
