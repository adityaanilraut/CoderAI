#!/usr/bin/env python3
"""Verify release archives and probe an installed wheel outside the checkout."""

from __future__ import annotations

import argparse
import ast
import email
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib
import venv
import zipfile

DIST_NAME = "coderai-agent"
ENTRY_POINTS = {
    "coderai = coderai.main:main",
    "cai = coderai.main:main",
}
REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_members(pattern: str) -> tuple[str, ...]:
    return tuple(
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "coderai").rglob(pattern))
        if path.is_file()
    )


RUNTIME_MEMBERS = _source_members("*.py")
BUNDLED_MEMBERS = (
    *_source_members("SKILL.md"),
    *_source_members("references/*.md"),
    # Mirrors [tool.setuptools.package-data] in pyproject.toml: prompt,
    # tool descriptions, and agent-spec data files shipped inside the wheel.
    "coderai/py.typed",
    "coderai/kaos/LICENSE",
    "coderai/kaos/NOTICE",
    *_source_members("prompt/templates/*.md"),
    *_source_members("tools/*/*.md"),
    *_source_members("agents/*/*.yaml"),
    *_source_members("agents/*/*.md"),
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
    if message.get("Requires-Python") != ">=3.12":
        raise SystemExit(f"{label} must declare Requires-Python: >=3.12")
    return version


def _verify_stamp(raw: bytes, expected_sha: str, *, label: str) -> str:
    build_id = None
    for node in ast.parse(raw.decode("utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BUILD_SHA" for target in node.targets
        ):
            build_id = ast.literal_eval(node.value)
    if not isinstance(build_id, str) or build_id.rsplit("@", 1)[-1] != expected_sha:
        raise SystemExit(f"{label} build identity {build_id!r} does not match {expected_sha!r}")
    return build_id


def _reject_generated_members(members: set[str], *, label: str) -> None:
    cache_directories = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
    generated = sorted(
        name
        for name in members
        if cache_directories.intersection(name.split("/"))
        or name.endswith((".pyc", ".pyo", "/.DS_Store"))
    )
    if generated:
        raise SystemExit(f"{label} must not contain generated caches: {', '.join(generated)}")


def _verify_wheel_archive(
    wheel: Path, *, expected_version: str | None, expected_sha: str | None = None
) -> str:
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        _reject_generated_members(members, label="Wheel")
        missing = sorted((set(RUNTIME_MEMBERS) | set(BUNDLED_MEMBERS)) - members)
        if missing:
            raise SystemExit("Wheel is missing runtime files: " + ", ".join(missing))
        if any(name.startswith("coderai/vendor/rg") for name in members):
            raise SystemExit("Universal wheel must not contain platform-specific rg assets")
        if expected_sha is not None:
            if "coderai/_build_info.py" not in members:
                raise SystemExit("Wheel is missing build source identity")
            _verify_stamp(archive.read("coderai/_build_info.py"), expected_sha, label="Wheel")
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


def _verify_sdist(sdist: Path, *, wheel_version: str, expected_sha: str | None = None) -> None:
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
        _reject_generated_members(members, label="Sdist")
        roots = {name.split("/", 1)[0] for name in members}
        if len(roots) != 1:
            raise SystemExit("Sdist must contain exactly one top-level directory")
        root = next(iter(roots))
        required = {
            "LICENSE",
            "README.md",
            "SECURITY.md",
            "MANIFEST.in",
            "pyproject.toml",
            "scripts/verify_wheel.py",
            "scripts/audit_dependencies.py",
            "docs/security/dependency-audit-policy.json",
            *RUNTIME_MEMBERS,
            *BUNDLED_MEMBERS,
        }
        missing = sorted(f"{root}/{name}" for name in required if f"{root}/{name}" not in members)
        if missing:
            raise SystemExit("Sdist is missing release files: " + ", ".join(missing))
        if expected_sha is not None:
            stamp_name = f"{root}/coderai/_build_info.py"
            stamp = archive.extractfile(stamp_name) if stamp_name in members else None
            if stamp is None:
                raise SystemExit("Sdist is missing build source identity")
            _verify_stamp(stamp.read(), expected_sha, label="Sdist")
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
import importlib.resources
import sys
from pathlib import Path
import coderai

assert Path(coderai.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), coderai.__file__
print("installed-package=" + str(Path(coderai.__file__).resolve()))

from coderai._version import __version__
from coderai.soul.session.manager import SessionManager
from coderai.utils.common.file_history import GitFileHistory
from coderai.tools.legacy.executor import ToolExecutor
from coderai.tools.legacy.registry import get_tool_registry

metadata = importlib.metadata.metadata('coderai-agent')
assert __version__ == metadata['Version'], (__version__, metadata['Version'])
tools = get_tool_registry().to_openai_schemas()
tool_names = {t['function']['name'] for t in tools}
assert {'bash', 'read', 'write', 'edit', 'AskUserQuestion', 'UpdatePlan', 'WebSearch'} <= tool_names
package_root = importlib.resources.files('coderai')
assert not package_root.joinpath('vendor/rg').is_file()
assert package_root.joinpath('skills/coderai-self-refer/SKILL.md').is_file()
print('core-probe-ok')
"""


def _search_probe() -> str:
    return """
import os
from pathlib import Path
from coderai.tools.file.grep import handle_grep_tool
from coderai.tools.file.glob import handle_glob_tool
from coderai.tools.legacy.types import ToolExecutionContext
from coderai.tools.file._search_common import resolve_rg_path

Path('src').mkdir(exist_ok=True)
Path('src/match.py').write_text('zero\\nwheel NEEDLE value\\nend\\n', encoding='utf-8')
Path('src/other.txt').write_text('unrelated\\n', encoding='utf-8')
ctx = ToolExecutionContext(session_id='wheel-search', project_root=str(Path.cwd()))
auto_backend = 'ripgrep' if resolve_rg_path() else 'python'
for backend in ('auto', 'python', 'auto_without_rg'):
    os.environ['CODERAI_SEARCH_BACKEND'] = backend
    if backend == 'auto_without_rg':
        os.environ['PATH'] = ''
        assert resolve_rg_path() is None
    grep = handle_grep_tool({'pattern': 'needle', 'include': '*.py', 'ignore_case': True}, ctx)
    assert grep.ok, grep
    assert grep.metadata['matches'] == [{'path': 'src/match.py', 'lineNumber': 2, 'line': 'wheel NEEDLE value'}], grep
    glob = handle_glob_tool({'pattern': '**/*.py'}, ctx)
    assert glob.ok and glob.metadata['paths'] == ['src/match.py'], glob
    empty = handle_grep_tool({'pattern': 'absent'}, ctx)
    assert empty.ok and empty.metadata['count'] == 0, empty
print('search-probe-ok;auto=' + auto_backend + ';forced=python;no-rg=python')
"""


def _verify_installed(
    wheel: Path,
    *,
    expected_version: str,
    install_dependencies: bool,
    system_site_packages: bool,
    expected_sha: str | None = None,
    audit: bool = False,
    audit_report: Path | None = None,
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
            # Python 3.12's bundled ensurepip version has known advisories.
            # Audit the actual installation after updating its bootstrap tooling.
            upgrade_deps=audit,
        ).create(environment)
        python = _venv_python(environment)
        if audit:
            dev = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
                "optional-dependencies"
            ]["dev"]
            audit_requirement = next(item for item in dev if item.startswith("pip-audit=="))
            _run_checked(
                [str(python), "-m", "pip", "install", audit_requirement],
                cwd=empty_project,
                env=os.environ.copy(),
            )
        install = [
            os.fspath(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--force-reinstall",
        ]
        if not install_dependencies:
            install.append("--no-deps")
        target = os.fspath(wheel)
        install.append(target)
        _run_checked(install, cwd=empty_project, env=os.environ.copy())

        child_environment = os.environ.copy()
        child_environment["HOME"] = os.fspath(empty_home)
        child_environment["USERPROFILE"] = os.fspath(empty_home)
        child_environment.pop("PYTHONPATH", None)
        child_environment.pop("CODERAI_BUILD_SHA", None)
        child_environment.pop("CODERAI_SEARCH_BACKEND", None)
        child_environment.pop("CODERAI_RG_PATH", None)
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

        search = _run_checked(
            [os.fspath(python), "-I", "-c", _search_probe()],
            cwd=empty_project,
            env=child_environment,
        )
        build_identity = _run_checked(
            [
                os.fspath(python),
                "-I",
                "-c",
                "from coderai.constant import get_build_sha; print(get_build_sha())",
            ],
            cwd=empty_project,
            env=child_environment,
        ).stdout.strip()
        if expected_sha is not None and build_identity.rsplit("@", 1)[-1] != expected_sha:
            raise SystemExit(
                f"Installed build identity {build_identity!r} does not match {expected_sha!r}"
            )
        audit_evidence = None
        if audit:
            audit_output = (audit_report or root / "dependency-audit.json").resolve()
            print("Installed-wheel core, CLI, search and source identity probes passed", flush=True)
            _run_checked(
                [
                    str(python),
                    str(REPO_ROOT / "scripts/audit_dependencies.py"),
                    "--output",
                    str(audit_output),
                ],
                cwd=empty_project,
                env=child_environment,
            )
            audit_evidence = json.loads(audit_output.read_text())
            _run_checked(
                [str(python), "-m", "pip", "check"], cwd=empty_project, env=child_environment
            )
        return {
            "dependency_audit": audit_evidence,
            "installation": "ok",
            "probe": "ok",
            "search": search.stdout.strip(),
            "build_identity": build_identity,
            "installed_package": probe_res.stdout.strip().splitlines()[0],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="Wheel path, or directory containing one wheel and sdist")
    parser.add_argument("--expected-sha", help="Require the exact full source SHA in all artifacts")
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
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit this exact installed dependency graph; block on findings/errors",
    )
    parser.add_argument(
        "--audit-report",
        type=Path,
        help="Preserve resolved graph/audit evidence even when the audit blocks",
    )
    args = parser.parse_args()
    if args.audit and (args.no_deps or args.system_site_packages or args.archive_only):
        parser.error("--audit requires a clean installation with dependencies")

    wheel, sdist = _resolve_archives(args.wheel)
    wheel_version = _verify_wheel_archive(
        wheel, expected_version=args.expected_version, expected_sha=args.expected_sha
    )
    if sdist is not None:
        _verify_sdist(sdist, wheel_version=wheel_version, expected_sha=args.expected_sha)
    if args.archive_only:
        print(
            json.dumps(
                {
                    "wheel": wheel.name,
                    "sdist": sdist.name if sdist else None,
                    "version": wheel_version,
                    "archive": "ok",
                    "expected_sha": args.expected_sha,
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
        expected_sha=args.expected_sha,
        audit=args.audit,
        audit_report=args.audit_report,
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
