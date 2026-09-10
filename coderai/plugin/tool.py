# Ported from coderai/core/plugin/tool.py - kimi structure (kimi_cli/plugin/tool.py).
"""Plugin tool execution (Kimi ``plugin/tool.py`` parity, slim).

Each declared tool runs as a subprocess with its parameters on stdin (JSON);
stdout is the result. Host credentials reach the subprocess as env vars at
runtime (never baked into files), so OAuth refreshes stay effective.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from coderai.plugin import PLUGIN_JSON, PluginSpec, PluginToolSpec, parse_plugin_json
from coderai.plugin.manager import get_plugins_dir
from coderai.core.tools.types import ToolResult

PLUGIN_TOOL_TIMEOUT_S = 120.0


def _clean_env() -> dict[str, str]:
    """Subprocess env without secret-bearing variables (Kimi ``get_clean_env``)."""
    scrub = ("KEY", "PASSWORD", "SECRET", "TOKEN")
    return {
        k: v for k, v in os.environ.items() if not any(marker in k.upper() for marker in scrub)
    }


def iter_plugin_tools(
    plugins_dir: Path | None = None,
) -> list[tuple[PluginSpec, PluginToolSpec, Path]]:
    """Yield ``(spec, tool, plugin_dir)`` for every declared plugin tool."""
    root = plugins_dir or get_plugins_dir()
    if not root.is_dir():
        return []
    found: list[tuple[PluginSpec, PluginToolSpec, Path]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / PLUGIN_JSON).is_file():
            continue
        try:
            spec = parse_plugin_json(child / PLUGIN_JSON)
        except Exception:
            continue
        for tool_spec in spec.tools:
            found.append((spec, tool_spec, child))
    return found


def plugin_tool_definitions(
    plugins_dir: Path | None = None, reserved: set[str] | None = None
) -> list[dict[str, Any]]:
    """OpenAI function definitions for plugin tools (conflict-skip vs builtins)."""
    taken = set(reserved or ())
    defs: list[dict[str, Any]] = []
    for spec, tool_spec, _child in iter_plugin_tools(plugins_dir):
        if tool_spec.name in taken:
            continue
        taken.add(tool_spec.name)
        parameters = tool_spec.parameters or {"type": "object", "properties": {}}
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": tool_spec.name,
                    "description": tool_spec.description
                    or f"Plugin tool '{tool_spec.name}' (from {spec.name}).",
                    "parameters": parameters,
                },
            }
        )
    return defs


def find_plugin_tool(
    name: str, plugins_dir: Path | None = None
) -> tuple[PluginSpec, PluginToolSpec, Path] | None:
    for spec, tool_spec, child in iter_plugin_tools(plugins_dir):
        if tool_spec.name == name:
            return spec, tool_spec, child
    return None


async def run_plugin_tool(
    name: str,
    args: dict[str, Any],
    *,
    plugins_dir: Path | None = None,
    host_values: dict[str, str] | None = None,
    timeout_s: float = PLUGIN_TOOL_TIMEOUT_S,
) -> ToolResult:
    """Execute a plugin tool by name."""
    located = find_plugin_tool(name, plugins_dir)
    if located is None:
        return ToolResult(ok=False, name=name, error=f"Unknown plugin tool: {name}")
    spec, tool_spec, child = located
    if not tool_spec.command:
        return ToolResult(ok=False, name=name, error=f"Plugin tool '{name}' has no command.")
    env = _clean_env()
    if spec.inject and host_values:
        for target_key, source_key in spec.inject.items():
            if source_key in host_values:
                env[target_key] = host_values[source_key]
    try:
        proc = await asyncio.create_subprocess_exec(
            *tool_spec.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(child),
            env=env,
        )
    except Exception as exc:
        return ToolResult(ok=False, name=name, error=f"Plugin tool '{name}' failed to start: {exc}")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(args or {}, ensure_ascii=False).encode("utf-8")),
            timeout=timeout_s,
        )
    except (asyncio.TimeoutError, TimeoutError):
        with suppress(Exception):
            proc.kill()
            await proc.wait()
        return ToolResult(
            ok=False, name=name, error=f"Plugin tool '{name}' timed out after {timeout_s}s."
        )
    except asyncio.CancelledError:
        with suppress(Exception):
            proc.kill()
            await proc.wait()
        raise
    output = stdout.decode("utf-8", errors="replace").strip()
    err_output = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = err_output or output or f"Exit code {proc.returncode}"
        return ToolResult(ok=False, name=name, error=f"Plugin tool '{name}' failed: {detail}")
    return ToolResult(ok=True, name=name, output=output)
