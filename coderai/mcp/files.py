"""Global + CLI MCP config overlays (Kimi ``cli/mcp.py`` parity, stdlib only).

Layering (lowest → highest):
  1. ``~/.coderai/mcp.json`` global file (Kimi: ``~/.kimi/mcp.json``)
  2. user + project ``mcpServers`` from settings files (see ``settings.py``)
  3. ``--mcp-config-file`` / ``--mcp-config`` CLI overlays (highest wins)

This module owns (1) and (3) parsing so both ``settings.py`` and the CLI
share one validator. No third-party deps: invalid JSON/shapes are tolerated
with an error string instead of raising.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_global_mcp_config_file() -> Path:
    """Return the global MCP config file path (``~/.coderai/mcp.json``)."""
    from coderai.share import get_share_dir

    return get_share_dir() / "mcp.json"


def _clean_servers(raw: Any) -> dict[str, dict[str, Any]]:
    """Extract valid ``{name: cfg}`` entries (cfg needs ``command`` or ``url``)."""
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("mcpServers", raw if any(isinstance(v, dict) for v in raw.values()) else {})
    if not isinstance(servers, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for name, cfg in servers.items():
        if not isinstance(name, str) or not name or not isinstance(cfg, dict):
            continue
        command = str(cfg.get("command") or "").strip()
        url = str(cfg.get("url") or "").strip()
        if not command and not url:
            continue
        cleaned[name] = cfg
    return cleaned


def try_load_mcp_servers_file(path: str | Path) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Load ``{name: cfg}`` from a JSON file. Returns ``(servers, error)``."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"cannot read MCP config file '{path}': {exc}"
    return parse_mcp_config_json(text, source=str(path))


def parse_mcp_config_json(text: str, *, source: str = "<config>") -> tuple[dict, str | None]:
    """Parse an MCP config JSON string. Returns ``(servers, error)``."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON in MCP config {source}: {exc}"
    if not isinstance(data, dict):
        return {}, f"invalid MCP config in {source}: top-level object required"
    return _clean_servers(data), None


def load_global_mcp_servers() -> dict[str, dict[str, Any]]:
    """Best-effort load of the global ``~/.coderai/mcp.json`` file."""
    path = get_global_mcp_config_file()
    if not path.is_file():
        return {}
    servers, _ = try_load_mcp_servers_file(path)
    return servers


def merge_server_cfg(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Merge one server config; overlay wins except ``env``/``headers`` merge."""
    merged = dict(base)
    for key, value in over.items():
        if key in ("env", "headers") and isinstance(value, dict) and isinstance(
            merged.get(key), dict
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def merge_mcp_servers_dicts(
    base: dict[str, dict[str, Any]] | None,
    *overlays: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Merge server dicts; later overlays win per server. ``None`` if all empty."""
    merged: dict[str, dict[str, Any]] = {}
    for layer in (base, *overlays):
        if not layer:
            continue
        for name, cfg in layer.items():
            if not isinstance(name, str) or not isinstance(cfg, dict):
                continue
            if name in merged:
                merged[name] = merge_server_cfg(merged[name], cfg)
            else:
                merged[name] = dict(cfg)
    return merged or None


def collect_cli_mcp_overlays(
    config_files: list[str] | None = None,
    config_jsons: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load ``--mcp-config-file`` / ``--mcp-config`` overlays.

    Returns ``(merged_servers, warnings)``; bad entries are skipped with a
    warning instead of aborting startup (Kimi raises; we warn to stay
    offline-safe and non-interactive friendly).
    """
    merged: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for path in config_files or []:
        servers, error = try_load_mcp_servers_file(path)
        if error:
            warnings.append(error)
            continue
        for name, cfg in servers.items():
            merged[name] = merge_server_cfg(merged[name], cfg) if name in merged else dict(cfg)
    for raw in config_jsons or []:
        servers, error = parse_mcp_config_json(raw, source="--mcp-config")
        if error:
            warnings.append(error)
            continue
        for name, cfg in servers.items():
            merged[name] = merge_server_cfg(merged[name], cfg) if name in merged else dict(cfg)
    # Env-bridged overlays (set by ``main()`` from argv for child reuse).
    env_files = os.getenv("CODERAI_MCP_CONFIG_FILES")
    if env_files and not config_files:
        for path in env_files.split(os.pathsep):
            path = path.strip()
            if not path:
                continue
            servers, error = try_load_mcp_servers_file(path)
            if error:
                warnings.append(error)
                continue
            for name, cfg in servers.items():
                merged[name] = merge_server_cfg(merged[name], cfg) if name in merged else dict(cfg)
    env_json = os.getenv("CODERAI_MCP_CONFIG_JSON")
    if env_json and not config_jsons:
        servers, error = parse_mcp_config_json(env_json, source="CODERAI_MCP_CONFIG_JSON")
        if error:
            warnings.append(error)
        else:
            for name, cfg in servers.items():
                merged[name] = merge_server_cfg(merged[name], cfg) if name in merged else dict(cfg)
    return merged, warnings
