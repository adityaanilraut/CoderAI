# Ported from coderai/cli/plugin_cmd.py - kimi structure (cli/plugin.py).
"""``coderai plugin`` subcommand (Kimi ``cli/plugin.py`` parity, stdlib only).

- ``install <directory | .zip | .zip URL | git URL>``
- ``list [--json]``
- ``remove <name>``
- ``info <name>``
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from coderai.plugin import PLUGIN_JSON, PluginError, parse_plugin_json
from coderai.plugin.manager import (
    collect_host_values,
    get_plugins_dir,
    install_plugin,
    list_plugins,
    remove_plugin,
)


def parse_git_url(target: str) -> tuple[str, str | None, str | None]:
    """Parse a git URL into (clone_url, subpath, branch)."""
    idx = target.find(".git/")
    if idx == -1 and target.endswith(".git"):
        return target, None, None
    if idx != -1:
        return target[: idx + 4], target[idx + 5 :].strip("/") or None, None
    parsed = urlparse(target)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return target, None, None
    clone_url = f"{parsed.scheme}://{parsed.netloc}/{'/'.join(segments[:2])}"
    rest = segments[2:]
    if rest and rest[0] == "-":
        rest = rest[1:]
    branch: str | None = None
    if len(rest) >= 2 and rest[0] == "tree":
        branch, rest = rest[1], rest[2:]
    return clone_url, "/".join(rest) or None, branch


def _extract_zip_to_plugin(zip_path: Path, tmp: Path) -> Path:
    """Extract a zip into tmp, guarding against path traversal; return plugin dir."""
    import zipfile

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if not (tmp / member).resolve().is_relative_to(tmp.resolve()):
                    raise PluginError(f"zip contains unsafe path: {member}")
            zf.extractall(tmp)
    except zipfile.BadZipFile as exc:
        raise PluginError(f"invalid zip archive: {exc}") from exc
    for candidate in [tmp, *sorted(tmp.iterdir())]:
        if candidate.is_dir() and (candidate / PLUGIN_JSON).exists():
            return candidate
    raise PluginError("No plugin.json found in zip")


def _resolve_source(target: str) -> tuple[Path, Path | None]:
    """Resolve a plugin source to (local_dir, tmp_to_cleanup)."""
    import requests

    parsed = urlparse(target)
    if parsed.scheme in ("http", "https") and parsed.path.lower().endswith(".zip"):
        tmp = Path(tempfile.mkdtemp(prefix="coderai-plugin-"))
        zip_path = tmp / "_download.zip"
        print(f"Downloading {target}...")
        try:
            with requests.get(target, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with zip_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PluginError(f"download failed: {exc}") from exc
        try:
            return _extract_zip_to_plugin(zip_path, tmp), tmp
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    if target.startswith(("https://", "git@", "http://")) and (
        ".git/" in target
        or target.endswith(".git")
        or "github.com/" in target
        or "gitlab.com/" in target
    ):
        clone_url, subpath, branch = parse_git_url(target)
        tmp = Path(tempfile.mkdtemp(prefix="coderai-plugin-"))
        print(f"Cloning {clone_url}...")
        clone_cmd = ["git", "clone", "--depth", "1"]
        if branch:
            clone_cmd += ["--branch", branch]
        clone_cmd += [clone_url, str(tmp / "repo")]
        result = subprocess.run(clone_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise PluginError(f"git clone failed: {result.stderr.strip()}")
        repo_root = tmp / "repo"
        if subpath:
            source = (repo_root / subpath).resolve()
            if not source.is_relative_to(repo_root.resolve()) or not source.is_dir():
                shutil.rmtree(tmp, ignore_errors=True)
                raise PluginError(f"subpath '{subpath}' not found in repository")
            if not (source / PLUGIN_JSON).exists():
                shutil.rmtree(tmp, ignore_errors=True)
                raise PluginError(f"no plugin.json in '{subpath}'")
            return source, tmp
        if (repo_root / PLUGIN_JSON).exists():
            return repo_root, tmp
        available = sorted(
            d.name for d in repo_root.iterdir() if d.is_dir() and (d / PLUGIN_JSON).exists()
        )
        shutil.rmtree(tmp, ignore_errors=True)
        if available:
            names = "\n".join(f"  - {n}" for n in available)
            raise PluginError(
                f"No plugin.json at repository root. Available plugins:\n{names}\n"
                f"Use: coderai plugin install <url>/<plugin-name>"
            )
        raise PluginError("No plugin.json found in repository")

    p = Path(target).expanduser().resolve()
    if p.is_file() and p.suffix == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="coderai-plugin-"))
        print(f"Extracting {p.name}...")
        try:
            return _extract_zip_to_plugin(p, tmp), tmp
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    if p.is_dir():
        return p, None
    raise PluginError(f"{target} is not a directory, zip file, zip URL, or git URL")


def cmd_plugin_install(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai plugin install <directory | .zip | .zip URL | git URL>")
        return 2
    try:
        source, tmp_dir = _resolve_source(argv[0])
    except PluginError as exc:
        print(f"Error: {exc}")
        return 1
    try:
        from coderai.llm import create_openai_client
        from coderai.config import resolve_current_settings

        merged = dict(resolve_current_settings("."))
        try:
            info = create_openai_client(".") or {}
            if isinstance(info, dict):
                merged.update(info)
        except Exception:
            pass
        host_values = collect_host_values(merged)
        if not host_values.get("api_key"):
            print(
                "Warning: no LLM provider configured; plugins needing API key "
                "injection will fail. Run 'coderai login' or configure a provider first."
            )
        spec = install_plugin(
            source=source, plugins_dir=get_plugins_dir(), host_values=host_values
        )
    except PluginError as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"Installed plugin '{spec.name}' v{spec.version}")
    if spec.runtime:
        print(f"  runtime: host={spec.runtime.host}, version={spec.runtime.host_version}")
    return 0


def cmd_plugin_list(argv: list[str]) -> int:
    as_json = bool(argv) and argv[0] == "--json"
    plugins = list_plugins(get_plugins_dir())
    if as_json:
        print(
            json.dumps(
                {
                    "pluginsDir": str(get_plugins_dir()),
                    "plugins": [p.model_dump() for p in plugins],
                },
                indent=2,
            )
        )
        return 0
    if not plugins:
        print("No plugins installed.")
        return 0
    for p in plugins:
        status = "installed" if p.runtime else "not configured"
        print(f"  {p.name} v{p.version} ({status})")
        if p.description:
            print(f"    {p.description}")
        for tool in p.tools:
            print(f"    tool: {tool.name}")
    return 0


def cmd_plugin_remove(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai plugin remove <name>")
        return 2
    try:
        remove_plugin(argv[0], get_plugins_dir())
    except PluginError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Removed plugin '{argv[0]}'")
    return 0


def cmd_plugin_info(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print("Usage: coderai plugin info <name>")
        return 2
    plugin_json = get_plugins_dir() / argv[0] / PLUGIN_JSON
    if not plugin_json.exists():
        print(f"Error: Plugin '{argv[0]}' not found")
        return 1
    try:
        spec = parse_plugin_json(plugin_json)
    except PluginError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Name:        {spec.name}")
    print(f"Version:     {spec.version}")
    print(f"Description: {spec.description or '(none)'}")
    print(f"Config file: {spec.config_file or '(none)'}")
    if spec.inject:
        print(f"Inject:      {', '.join(f'{k} <- {v}' for k, v in spec.inject.items())}")
    for tool in spec.tools:
        print(f"Tool:        {tool.name} — {tool.description or '(no description)'}")
    if spec.runtime:
        print(f"Runtime:     host={spec.runtime.host}, version={spec.runtime.host_version}")
    else:
        print("Runtime:     (not installed via host)")
    return 0


def run_plugin(argv: list[str]) -> int:
    """Dispatch ``coderai plugin ...``. Returns a process exit code."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            "Usage: coderai plugin <install|list|remove|info> [options]\n"
            "\n"
            "  install <src>  Install from a directory, .zip, .zip URL, or git URL\n"
            "  list [--json]  List installed plugins\n"
            "  remove <name>  Remove an installed plugin\n"
            "  info <name>    Show plugin details\n"
        )
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "install":
        return cmd_plugin_install(rest)
    if sub == "list":
        return cmd_plugin_list(rest)
    if sub == "remove":
        return cmd_plugin_remove(rest)
    if sub == "info":
        return cmd_plugin_info(rest)
    print(f"Unknown 'plugin' subcommand: {sub} (expected install|list|remove|info).")
    return 2
