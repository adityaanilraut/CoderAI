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
ENTRY_POINT = "coderAI = coderAI.cli:main"
ADVERTISED_EXTRAS = ("semantic", "local-embeddings", "web", "browser")
BUILTIN_PERSONAS = (
    "architect",
    "build-error-resolver",
    "code-reviewer",
    "planner",
    "security-reviewer",
    "tdd-guide",
)
BUILTIN_SKILLS = ("security-audit", "tdd-workflow")
BUILTIN_RULES = ("001-common-principles", "101-python-standards")
PROMPTS = (
    "browser.mdx",
    "desktop.mdx",
    "interaction.mdx",
    "intro.mdx",
    "output_style.mdx",
    "runtime.mdx",
    "tail.mdx",
)
CORE_TOOL_NAMES = (
    "apply_diff",
    "delegate_task",
    "git_status",
    "grep",
    "manage_tasks",
    "mcp_connect",
    "read_file",
    "read_url",
    "semantic_search",
    "submit_plan",
    "undo",
    "use_skill",
    "write_file",
)
CLI_HELP_PATHS = (
    (),
    ("chat",),
    ("run",),
    ("plan",),
    ("plan", "create"),
    ("plan", "show"),
    ("plan", "edit"),
    ("plan", "apply"),
    ("plan", "answer"),
    ("plan", "approve"),
    ("plan", "execute"),
    ("mcp",),
    ("mcp", "add"),
    ("mcp", "list"),
    ("mcp", "get"),
    ("mcp", "remove"),
    ("mcp", "enable"),
    ("mcp", "disable"),
    ("mcp", "approve"),
    ("mcp", "reject"),
    ("mcp", "catalog"),
    ("mcp", "import"),
    ("mcp", "debug"),
    ("mcp", "login"),
    ("mcp", "logout"),
    ("mcp", "resources"),
    ("mcp", "prompts"),
    ("skills",),
    ("skills", "install"),
    ("skills", "list"),
    ("skills", "remove"),
    ("config",),
    ("config", "show"),
    ("config", "set"),
    ("config", "reset"),
    ("history",),
    ("history", "list"),
    ("history", "rename"),
    ("history", "tag"),
    ("history", "export"),
    ("history", "clear"),
    ("history", "delete"),
    ("tasks",),
    ("tasks", "list"),
    ("models",),
    ("set-model",),
    ("info",),
    ("status",),
    ("doctor",),
    ("cost",),
    ("setup",),
    ("index",),
    ("search",),
)


def _runtime_members() -> set[str]:
    return {
        "coderAI/_version.py",
        "coderAI/py.typed",
        "coderAI/mcp_servers/git_extended.py",
        *(f"coderAI/prompts/{name}" for name in PROMPTS),
        *(f"coderAI/assets/agents/{name}.md" for name in BUILTIN_PERSONAS),
        *(f"coderAI/assets/skills/{name}/SKILLS.md" for name in BUILTIN_SKILLS),
        *(f"coderAI/assets/rules/{name}.md" for name in BUILTIN_RULES),
        "coderAI/assets/starter/CODERAI.md",
    }


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
    extras = set(message.get_all("Provides-Extra") or [])
    missing_extras = sorted(set(ADVERTISED_EXTRAS) - extras)
    if missing_extras:
        raise SystemExit(f"{label} metadata is missing extras: {', '.join(missing_extras)}")
    requirements = message.get_all("Requires-Dist") or []
    for package, extra in (
        ("chromadb", "semantic"),
        ("sentence-transformers", "local-embeddings"),
        ("pypdf", "web"),
        ("playwright", "browser"),
    ):
        if not any(
            req.lower().startswith(package) and f'extra == "{extra}"' in req for req in requirements
        ):
            raise SystemExit(f"{label} metadata does not bind {package} to extra {extra}")
    return version


def _verify_wheel_archive(wheel: Path, *, expected_version: str | None) -> str:
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        missing = sorted(_runtime_members() - members)
        if missing:
            raise SystemExit("Wheel is missing runtime files: " + ", ".join(missing))
        metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in members if name.endswith(".dist-info/entry_points.txt")]
        if len(metadata_names) != 1 or len(entry_names) != 1:
            raise SystemExit("Wheel must contain exactly one METADATA and entry_points.txt")
        metadata = _message(archive.read(metadata_names[0]), label="Wheel")
        entry_points = archive.read(entry_names[0]).decode("utf-8")
    if ENTRY_POINT not in {line.strip() for line in entry_points.splitlines()}:
        raise SystemExit(f"Wheel console entry point is missing: {ENTRY_POINT}")
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
            "scripts/verify_wheel.py",
            "docs/ARCHITECTURE.md",
            "docs/CHAT_EVENTS.md",
            "docs/COMMANDS.md",
            "docs/COMPETITIVE_AUDIT_REMEDIATION_PLAN.md",
            *_runtime_members(),
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


def _verify_cli(
    environment: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    expected_version: str,
) -> None:
    executable = _venv_script(environment, "coderAI")
    version = _run_checked([os.fspath(executable), "--version"], cwd=cwd, env=env)
    if version.stdout.strip() != f"CoderAI version {expected_version}":
        raise SystemExit(f"Unexpected console version output: {version.stdout!r}")
    for path in CLI_HELP_PATHS:
        _run_checked([os.fspath(executable), *path, "--help"], cwd=cwd, env=env)
    skills = _run_checked(
        [os.fspath(executable), "skills", "list", "--scope", "all"],
        cwd=cwd,
        env=env,
    )
    for name in BUILTIN_SKILLS:
        if name not in skills.stdout:
            raise SystemExit(f"Installed CLI did not list built-in skill {name!r}")


def _core_probe(extras: tuple[str, ...]) -> str:
    return f"""
import asyncio
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from coderAI import __version__
from coderAI.assets.manifest import asset_text, verify_builtin_assets
from coderAI.core.personas import get_available_persona_descriptors
from coderAI.prompts.compose import compose_default_system_prompt
from coderAI.skills.skill_manager import discover_local_skills
from coderAI.tools.base import ToolRegistry
from coderAI.tools.discovery import discover_tools
from coderAI.tools.mcp import bundled_mcp_servers
from coderAI.tui.commands import _do_init_project

extras = {extras!r}
metadata = importlib.metadata.metadata('coderai-agent')
assert __version__ == metadata['Version'], (__version__, metadata['Version'])
entry_points = importlib.metadata.entry_points(group='console_scripts')
assert any(ep.name == 'coderAI' and ep.value == 'coderAI.cli:main' for ep in entry_points)

manifest = verify_builtin_assets()
personas = get_available_persona_descriptors('.')
skills = discover_local_skills('.')
assert [item.name for item in personas] == manifest['personas'], personas
assert {{item.scope for item in personas}} == {{'builtin'}}, personas
assert [item.name for item in skills] == manifest['skills'], skills
assert {{item.source for item in skills}} == {{'builtin'}}, skills
assert 'Project Guidance for CoderAI' in asset_text('starter', 'CODERAI.md')

prompt = compose_default_system_prompt(ToolRegistry())
assert 'CoderAI' in prompt and len(prompt) > 500
registry = ToolRegistry()
discover_tools(registry)
missing_tools = sorted(set({CORE_TOOL_NAMES!r}) - set(registry.tools))
assert not missing_tools, missing_tools

created_dirs, created_files, skipped, error = _do_init_project(Path('.'))
assert error is None, error
assert not skipped, skipped
assert {{'CODERAI.md', '.coderAI/agents/planner.md', '.coderAI/rules/001-common-principles.md'}} <= set(created_files)
assert {{'.coderAI/agents', '.coderAI/skills', '.coderAI/rules'}} <= set(created_dirs)

server = bundled_mcp_servers()['git_extended']
command = [server['command'], *server['args']]
requests = '\\n'.join((
    json.dumps({{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {{}}}}),
    json.dumps({{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {{}}}}),
)) + '\\n'
completed = subprocess.run(command, input=requests, text=True, capture_output=True, check=True)
replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
assert replies[0]['result']['serverInfo']['name'] == 'git_extended', replies
git_tools = {{tool['name'] for tool in replies[1]['result']['tools']}}
assert {{'git_push', 'git_rebase', 'git_cherry_pick', 'git_tag'}} <= git_tools, git_tools

if 'semantic' in extras:
    import chromadb
    assert chromadb is not None
if 'local-embeddings' in extras:
    import sentence_transformers
    from coderAI.embeddings.local import SentenceTransformerEmbeddingProvider
    class FakeEncoder:
        def get_sentence_embedding_dimension(self): return 2
        def encode(self, texts, **kwargs): return [[1.0, 0.0] for _ in texts]
    original = sentence_transformers.SentenceTransformer
    sentence_transformers.SentenceTransformer = lambda *args, **kwargs: FakeEncoder()
    try:
        provider = SentenceTransformerEmbeddingProvider('offline-smoke-model')
        assert provider.dimension() == 2
        assert asyncio.run(provider.embed(['probe'])) == [[1.0, 0.0]]
    finally:
        sentence_transformers.SentenceTransformer = original
if 'web' in extras:
    import pypdf
    from coderAI.tools.web._html import _extract_pdf_text
    class FakePage:
        def extract_text(self): return 'installed wheel PDF probe'
    original = pypdf.PdfReader
    pypdf.PdfReader = lambda stream: type('Reader', (), {{'pages': [FakePage()]}})()
    try:
        assert _extract_pdf_text(b'%PDF-probe') == 'installed wheel PDF probe'
    finally:
        pypdf.PdfReader = original
if 'browser' in extras:
    import playwright.async_api
    from coderAI.tools import browser
    assert browser._check_playwright() is None
    browser_names = {{name for name in registry.tools if name.startswith('browser_')}}
    assert len(browser_names) == 10, browser_names

print(json.dumps({{
    'version': __version__,
    'manifest': manifest,
    'extras': list(extras),
    'tool_count': len(registry.tools),
    'git_extended_tools': len(git_tools),
}}))
"""


def _browser_probe() -> str:
    return """
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as manager:
        browser = await manager.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<main><h1>CoderAI wheel browser probe</h1></main>')
        assert await page.locator('h1').inner_text() == 'CoderAI wheel browser probe'
        await browser.close()

asyncio.run(main())
print('browser-runtime-ok')
"""


def _verify_installed(
    wheel: Path,
    *,
    expected_version: str,
    extras: tuple[str, ...],
    install_browser: bool,
    install_dependencies: bool,
    system_site_packages: bool,
) -> dict[str, object]:
    unknown = sorted(set(extras) - set(ADVERTISED_EXTRAS))
    if unknown:
        raise SystemExit("Unknown optional extra(s): " + ", ".join(unknown))
    if install_browser and "browser" not in extras:
        raise SystemExit("--install-browser requires --extra browser")
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
        if extras:
            target = f"{target}[{','.join(extras)}]"
        install.append(target)
        _run_checked(install, cwd=empty_project, env=os.environ.copy())

        child_environment = os.environ.copy()
        child_environment["HOME"] = os.fspath(empty_home)
        child_environment["USERPROFILE"] = os.fspath(empty_home)
        child_environment.pop("PYTHONPATH", None)
        child_environment["PYTHONNOUSERSITE"] = "1"
        _verify_cli(
            environment,
            cwd=empty_project,
            env=child_environment,
            expected_version=expected_version,
        )
        completed = _run_checked(
            [os.fspath(python), "-I", "-c", _core_probe(extras)],
            cwd=empty_project,
            env=child_environment,
        )
        result = json.loads(completed.stdout.strip())
        if install_browser:
            _run_checked(
                [os.fspath(python), "-m", "playwright", "install", "chromium"],
                cwd=empty_project,
                env=child_environment,
            )
            _run_checked(
                [os.fspath(python), "-I", "-c", _browser_probe()],
                cwd=empty_project,
                env=child_environment,
            )
            result["browser_runtime"] = "ok"
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="Wheel path, or directory containing one wheel and sdist")
    parser.add_argument(
        "--expected-version",
        help="Require wheel, sdist, and installed runtime to use this exact version",
    )
    parser.add_argument(
        "--extra",
        action="append",
        choices=ADVERTISED_EXTRAS,
        default=[],
        help="Install and probe one advertised optional extra (repeatable)",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="Download Chromium and run a local Playwright launch probe",
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
        extras=tuple(dict.fromkeys(args.extra)),
        install_browser=args.install_browser,
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
