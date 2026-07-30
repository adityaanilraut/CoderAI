"""Curated MCP server presets for ``coderAI mcp add --preset`` / ``mcp catalog``."""

from __future__ import annotations

from typing import Any

# Templates only — not bundled processes. Users still need network/npx/auth.
MCP_PRESETS: dict[str, dict[str, Any]] = {
    "filesystem": {
        "description": "Read/write files under a directory (official filesystem server).",
        "entry": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "${CODERAI_PROJECT_DIR:-.}"],
        },
        "required_env": [],
    },
    "github": {
        "description": "GitHub issues, PRs, and repos via the official GitHub MCP server.",
        "entry": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"},
        },
        "required_env": ["GITHUB_TOKEN"],
    },
    "fetch": {
        "description": "Fetch URL contents as markdown (official fetch server).",
        "entry": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-fetch"],
        },
        "required_env": [],
    },
    "memory": {
        "description": "Persistent knowledge graph memory (official memory server).",
        "entry": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
        },
        "required_env": [],
    },
    "context7": {
        "description": "Up-to-date library docs via Context7 remote MCP.",
        "entry": {
            "transport": "http",
            "url": "https://mcp.context7.com/mcp",
        },
        "required_env": [],
    },
    "sentry": {
        "description": "Sentry issues and projects (remote MCP; use `coderAI mcp login`).",
        "entry": {
            "transport": "http",
            "url": "https://mcp.sentry.dev/mcp",
        },
        "required_env": [],
    },
}


def list_presets() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, meta in sorted(MCP_PRESETS.items()):
        rows.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "transport": meta.get("entry", {}).get("transport", "stdio"),
                "required_env": list(meta.get("required_env") or []),
            }
        )
    return rows


def get_preset(name: str) -> dict[str, Any]:
    """Return a deep-ish copy of the preset entry + metadata, or raise KeyError."""
    meta = MCP_PRESETS[name]
    entry = dict(meta["entry"])
    if "args" in entry:
        entry["args"] = list(entry["args"])
    if "env" in entry:
        entry["env"] = dict(entry["env"])
    return {
        "name": name,
        "description": meta.get("description", ""),
        "entry": entry,
        "required_env": list(meta.get("required_env") or []),
    }
