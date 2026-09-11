# Ported from coderai/core/plugin/manager.py - kimi structure (kimi_cli/plugin/manager.py).
"""Plugin installation, removal, and listing (Kimi ``plugin/manager.py`` parity)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from coderai.plugin import (
    PLUGIN_JSON,
    PluginError,
    PluginSpec,
    host_runtime,
    inject_config,
    parse_plugin_json,
    write_runtime,
)
from coderai.share import get_share_dir


def get_plugins_dir() -> Path:
    """Return the plugins installation directory (``~/.coderai/plugins/``)."""
    return get_share_dir() / "plugins"


def collect_host_values(source: dict[str, Any]) -> dict[str, str]:
    """Collect injectable host values (api_key, base_url).

    ``source`` is usually ``{**resolved_settings, **client_info}`` so both
    the static key and the OAuth-resolved key are visible. Prefers the OAuth
    access token when the active provider is OAuth-backed (Kimi parity).
    """
    api_key = str(source.get("apiKey") or source.get("api_key") or "")
    oauth_key = str(source.get("oauthKey") or "")
    if oauth_key:
        try:
            from coderai.auth.oauth import OAuthManager

            resolved = OAuthManager([oauth_key]).resolve_api_key(api_key, oauth_key)
            if resolved:
                api_key = resolved
        except Exception:
            pass
    values: dict[str, str] = {}
    if api_key:
        values["api_key"] = api_key
    base_url = str(source.get("baseURL") or source.get("base_url") or "")
    if base_url:
        values["base_url"] = base_url
    return values


def _validate_name(name: str, plugins_dir: Path) -> Path:
    """Resolve and validate a plugin name, returning the safe destination path."""
    dest = (plugins_dir / name).resolve()
    if not dest.is_relative_to(plugins_dir.resolve()):
        raise PluginError(f"Invalid plugin name: {name}")
    if not name or name in (".", ".."):
        raise PluginError(f"Invalid plugin name: {name}")
    return dest


def install_plugin(
    *,
    source: Path,
    plugins_dir: Path,
    host_values: dict[str, str],
) -> PluginSpec:
    """Install a plugin from a source directory (staged, atomic swap).

    A failed upgrade never destroys the previous installation.
    """
    source_plugin_json = source / PLUGIN_JSON
    if not source_plugin_json.exists():
        raise PluginError(f"No plugin.json found in {source}")
    spec = parse_plugin_json(source_plugin_json)
    dest = _validate_name(spec.name, plugins_dir)

    plugins_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{spec.name}-", dir=plugins_dir))
    try:
        staging_plugin = staging / spec.name
        shutil.copytree(source, staging_plugin)
        inject_config(staging_plugin, spec, host_values)
        write_runtime(staging_plugin, host_runtime())
        if dest.exists():
            shutil.rmtree(dest)
        staging_plugin.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return parse_plugin_json(dest / PLUGIN_JSON)


def refresh_plugin_configs(plugins_dir: Path, host_values: dict[str, str]) -> None:
    """Re-inject host values into installed plugin configs (startup refresh)."""
    if not plugins_dir.is_dir():
        return
    for child in sorted(plugins_dir.iterdir()):
        plugin_json = child / PLUGIN_JSON
        if not child.is_dir() or not plugin_json.is_file():
            continue
        try:
            spec = parse_plugin_json(plugin_json)
            if spec.inject and spec.config_file:
                inject_config(child, spec, host_values)
        except Exception:
            continue


def list_plugins(plugins_dir: Path) -> list[PluginSpec]:
    """List all installed plugins."""
    if not plugins_dir.is_dir():
        return []
    plugins: list[PluginSpec] = []
    for child in sorted(plugins_dir.iterdir()):
        plugin_json = child / PLUGIN_JSON
        if child.is_dir() and plugin_json.is_file():
            try:
                plugins.append(parse_plugin_json(plugin_json))
            except PluginError:
                continue
    return plugins


def remove_plugin(name: str, plugins_dir: Path) -> None:
    """Remove an installed plugin."""
    dest = _validate_name(name, plugins_dir)
    if not dest.exists():
        raise PluginError(f"Plugin '{name}' not found in {plugins_dir}")
    shutil.rmtree(dest)
