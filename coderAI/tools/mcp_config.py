"""MCP config scopes, ${VAR} expansion, and project-trust approvals.

Config locations (Claude Code-compatible project file):

* **user** — ``~/.coderAI/mcp_servers.json``
* **project** — ``.mcp.json`` at the project root (``mcpServers`` map)
* **local** — ``~/.coderAI/mcp_local.json`` → ``projects[<abs-root>].mcpServers``

Merge precedence for the same name: local → project → user → bundled.
Project servers require workspace trust and an explicit approve (or prior
approval hash match) before they are eligible for autoconnect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from collections.abc import Mapping

from coderAI.system.fsperms import atomic_write_json
from coderAI.system.proc import scrub_env

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from coderAI.tools.mcp import MCPClient

# ${VAR} or ${VAR:-default} — Claude/OpenCode-style substitution.
_ENV_EXPAND_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

MCP_SCOPES = ("user", "project", "local")
PROJECT_MCP_FILENAME = ".mcp.json"


def mcp_local_path() -> Path:
    from coderAI.system.config import config_manager

    return config_manager.config_dir / "mcp_local.json"


def mcp_approvals_path() -> Path:
    from coderAI.system.config import config_manager

    return config_manager.config_dir / "mcp_project_approvals.json"


def project_mcp_path(project_root: Optional[str | Path] = None) -> Path:
    root = Path(project_root or ".").resolve()
    return root / PROJECT_MCP_FILENAME


def expand_env_string(value: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` using *environ* (default: ``os.environ``)."""
    env = os.environ if environ is None else environ

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        logger.warning("MCP config references unset environment variable ${%s}", name)
        return match.group(0)

    return _ENV_EXPAND_RE.sub(_repl, value)


def expand_env_value(value: Any, environ: Optional[Mapping[str, str]] = None) -> Any:
    """Recursively expand strings in dicts/lists; leave other types unchanged."""
    if isinstance(value, str):
        return expand_env_string(value, environ)
    if isinstance(value, list):
        return [expand_env_value(v, environ) for v in value]
    if isinstance(value, dict):
        return {k: expand_env_value(v, environ) for k, v in value.items()}
    return value


def resolve_mcp_cwd(
    cwd: Optional[str],
    *,
    project_root: Optional[str | Path] = None,
) -> Path:
    """Resolve config ``cwd`` relative to the project root (default: ``.``)."""
    root = Path(project_root or ".").resolve()
    if not cwd:
        return root
    expanded = expand_env_string(str(cwd))
    path = Path(expanded)
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()
    return path


def build_stdio_env(
    config_env: Optional[Mapping[str, str]],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Scrub host env, then overlay only keys explicitly listed in config ``env``.

    Values in *config_env* are expanded against the host environment so users
    can write ``"TOKEN": "${GITHUB_TOKEN}"`` without dumping the full unsanitized
    environ into the child.
    """
    child = scrub_env()
    if not config_env:
        return child
    host = os.environ if environ is None else environ
    for key, raw in config_env.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(raw, str):
            raw = str(raw)
        child[key] = expand_env_string(raw, host)
    return child


def normalize_server_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize Claude/Cursor-style entries into CoderAI's transport schema."""
    entry = dict(raw)
    # Claude uses ``type``; CoderAI historically uses ``transport``.
    type_val = entry.pop("type", None)
    if type_val and "transport" not in entry:
        mapping = {
            "stdio": "stdio",
            "local": "stdio",
            "sse": "sse",
            "http": "http",
            "streamable-http": "http",
            "remote": "http",
        }
        entry["transport"] = mapping.get(str(type_val).lower(), str(type_val).lower())
    # OpenCode uses ``environment`` / ``enabled`` / ``command`` as argv list.
    if "environment" in entry and "env" not in entry:
        entry["env"] = entry.pop("environment")
    if "enabled" in entry and "disabled" not in entry:
        entry["disabled"] = not bool(entry.pop("enabled"))
    elif "enabled" in entry:
        entry.pop("enabled", None)
    cmd = entry.get("command")
    if isinstance(cmd, list) and cmd:
        entry["command"] = str(cmd[0])
        rest = [str(a) for a in cmd[1:]]
        existing_args = entry.get("args")
        if not isinstance(existing_args, list) or not existing_args:
            entry["args"] = rest
        else:
            entry["args"] = rest + [str(a) for a in existing_args]
    transport = entry.get("transport", "stdio")
    if transport not in ("stdio", "sse", "http"):
        entry["transport"] = "stdio"
    return entry


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Failed to read %s; treating as empty", path, exc_info=True)
        return {}


def load_user_mcp_servers() -> dict[str, Any]:
    from coderAI.tools.mcp import load_mcp_servers

    return load_mcp_servers()


def load_project_mcp_servers(
    project_root: Optional[str | Path] = None,
) -> dict[str, dict[str, Any]]:
    """Load ``.mcp.json`` servers (empty if missing). Does not apply trust."""
    path = project_mcp_path(project_root)
    data = _load_json_object(path)
    servers = data.get("mcpServers", data.get("mcp", {}))
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if isinstance(name, str) and isinstance(entry, dict):
            normalized = normalize_server_entry(entry)
            normalized["_scope"] = "project"
            out[name] = normalized
    return out


def load_local_mcp_servers(project_root: Optional[str | Path] = None) -> dict[str, dict[str, Any]]:
    root = str(Path(project_root or ".").resolve())
    data = _load_json_object(mcp_local_path())
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        return {}
    project_entry = projects.get(root, {})
    if not isinstance(project_entry, dict):
        return {}
    servers = project_entry.get("mcpServers", {})
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if isinstance(name, str) and isinstance(entry, dict):
            normalized = normalize_server_entry(entry)
            normalized["_scope"] = "local"
            out[name] = normalized
    return out


def set_local_mcp_disabled(
    name: str,
    disabled: bool,
    *,
    project_root: Optional[str | Path] = None,
) -> bool:
    """Flip ``disabled`` on a local-scope server. Returns ``False`` if absent."""
    root = str(Path(project_root or ".").resolve())
    path = mcp_local_path()
    data = _load_json_object(path)
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    bucket = projects.get(root)
    if not isinstance(bucket, dict):
        return False
    servers = bucket.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    entry = servers.get(name)
    if not isinstance(entry, dict):
        return False
    if disabled:
        entry["disabled"] = True
    else:
        entry.pop("disabled", None)
    atomic_write_json(path, data)
    return True


def entry_content_hash(entry: Mapping[str, Any]) -> str:
    """Stable hash of a server entry for re-approval when project config changes."""
    cleaned = {k: v for k, v in entry.items() if not str(k).startswith("_")}
    payload = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_approvals() -> dict[str, Any]:
    return _load_json_object(mcp_approvals_path())


def _save_approvals(data: dict[str, Any]) -> None:
    atomic_write_json(mcp_approvals_path(), data)


def get_project_approvals(project_root: Optional[str | Path] = None) -> dict[str, Any]:
    root = str(Path(project_root or ".").resolve())
    store = _load_approvals()
    entry = store.get(root, {})
    if not isinstance(entry, dict):
        return {"enabled": {}, "disabled": []}
    enabled = entry.get("enabled", {})
    disabled = entry.get("disabled", [])
    if not isinstance(enabled, dict):
        enabled = {}
    if not isinstance(disabled, list):
        disabled = []
    return {"enabled": enabled, "disabled": list(disabled)}


def set_project_approval(
    name: str,
    *,
    approved: bool,
    entry: Optional[Mapping[str, Any]] = None,
    project_root: Optional[str | Path] = None,
) -> None:
    """Record approve/reject for a project-scoped MCP server."""
    root = str(Path(project_root or ".").resolve())
    store = _load_approvals()
    bucket = store.setdefault(root, {"enabled": {}, "disabled": []})
    if not isinstance(bucket, dict):
        bucket = {"enabled": {}, "disabled": []}
        store[root] = bucket
    enabled = bucket.setdefault("enabled", {})
    disabled = bucket.setdefault("disabled", [])
    if not isinstance(enabled, dict):
        enabled = {}
        bucket["enabled"] = enabled
    if not isinstance(disabled, list):
        disabled = []
        bucket["disabled"] = disabled

    if approved:
        if name in disabled:
            disabled[:] = [n for n in disabled if n != name]
        digest = entry_content_hash(entry or {})
        enabled[name] = digest
    else:
        enabled.pop(name, None)
        if name not in disabled:
            disabled.append(name)
    _save_approvals(store)


def project_server_status(
    name: str,
    entry: Mapping[str, Any],
    *,
    project_root: Optional[str | Path] = None,
) -> str:
    """Return ``approved``, ``rejected``, or ``pending`` for a project server."""
    approvals = get_project_approvals(project_root)
    if name in approvals["disabled"]:
        return "rejected"
    enabled = approvals["enabled"]
    digest = entry_content_hash(entry)
    stored = enabled.get(name)
    if stored == digest:
        return "approved"
    return "pending"


def effective_mcp_servers(
    *,
    project_root: Optional[str | Path] = None,
    workspace_trusted: Optional[bool] = None,
) -> dict[str, Any]:
    """Merge scopes into a single ``mcpServers`` map with metadata.

    Each entry may include ``_scope`` (``user``|``project``|``local``|``bundled``)
    and ``_approval`` (``approved``|``pending``|``rejected``) for project servers.
    Servers with ``_approval`` of ``pending`` or ``rejected`` keep ``disabled``
    semantics for autoconnect (skipped) unless the caller opts in.
    """
    from coderAI.tools.mcp import bundled_mcp_servers, load_mcp_servers

    root = Path(project_root or ".").resolve()
    if workspace_trusted is None:
        try:
            from coderAI.system.trust import workspace_trust

            workspace_trusted = workspace_trust.is_trusted(root)
        except Exception:
            workspace_trusted = False

    merged: dict[str, dict[str, Any]] = {}

    # Lowest precedence: bundled
    for name, entry in bundled_mcp_servers().items():
        e = dict(entry)
        e["_scope"] = "bundled"
        merged[name] = e

    # User
    user_data = load_mcp_servers()
    user_servers = user_data.get("mcpServers", {})
    if isinstance(user_servers, dict):
        for name, entry in user_servers.items():
            if isinstance(name, str) and isinstance(entry, dict):
                e = normalize_server_entry(entry)
                e["_scope"] = "user"
                merged[name] = e

    # Project (trusted only)
    if workspace_trusted:
        for name, entry in load_project_mcp_servers(root).items():
            e = dict(entry)
            status = project_server_status(name, e, project_root=root)
            e["_approval"] = status
            e["_scope"] = "project"
            if status != "approved":
                # Keep visible but not autoconnected until approved.
                e = dict(e)
                e["disabled"] = True
                e["_connect_blocked"] = status
            merged[name] = e

    # Local (highest)
    for name, entry in load_local_mcp_servers(root).items():
        e = dict(entry)
        e["_scope"] = "local"
        merged[name] = e

    return {"mcpServers": merged}


def save_mcp_server_to_scope(
    name: str,
    entry: dict[str, Any],
    scope: str = "user",
    *,
    project_root: Optional[str | Path] = None,
) -> Path:
    """Persist a server entry into the given scope. Returns the file written."""
    from coderAI.tools.mcp import load_mcp_servers, save_mcp_servers

    if scope not in MCP_SCOPES:
        raise ValueError(f"Invalid MCP scope {scope!r}; expected one of {MCP_SCOPES}")

    cleaned = {k: v for k, v in entry.items() if not str(k).startswith("_")}

    if scope == "user":
        data = load_mcp_servers()
        servers = data.setdefault("mcpServers", {})
        servers[name] = cleaned
        save_mcp_servers(data)
        from coderAI.tools.mcp import mcp_servers_path

        return mcp_servers_path()

    if scope == "project":
        path = project_mcp_path(project_root)
        data = _load_json_object(path)
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        servers[name] = cleaned
        atomic_write_json(path, data)
        # Auto-approve servers the user just added to the project file themselves.
        set_project_approval(name, approved=True, entry=cleaned, project_root=project_root)
        return path

    # local
    root = str(Path(project_root or ".").resolve())
    path = mcp_local_path()
    data = _load_json_object(path)
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    bucket = projects.setdefault(root, {"mcpServers": {}})
    if not isinstance(bucket, dict):
        bucket = {"mcpServers": {}}
        projects[root] = bucket
    servers = bucket.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        bucket["mcpServers"] = servers
    servers[name] = cleaned
    atomic_write_json(path, data)
    return path


def remove_mcp_server_from_scope(
    name: str,
    scope: Optional[str] = None,
    *,
    project_root: Optional[str | Path] = None,
) -> tuple[bool, str]:
    """Remove *name* from *scope* (or first scope that contains it).

    Returns ``(removed, scope_or_reason)``.
    """
    from coderAI.tools.mcp import load_mcp_servers, save_mcp_servers

    root = Path(project_root or ".").resolve()
    scopes = [scope] if scope else ["local", "project", "user"]

    for sc in scopes:
        if sc == "user":
            data = load_mcp_servers()
            servers = data.get("mcpServers", {})
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                save_mcp_servers(data)
                return True, "user"
        elif sc == "project":
            path = project_mcp_path(root)
            data = _load_json_object(path)
            servers = data.get("mcpServers", {})
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                atomic_write_json(path, data)
                return True, "project"
        elif sc == "local":
            path = mcp_local_path()
            data = _load_json_object(path)
            projects = data.get("projects", {})
            if not isinstance(projects, dict):
                continue
            bucket = projects.get(str(root), {})
            if not isinstance(bucket, dict):
                continue
            servers = bucket.get("mcpServers", {})
            if isinstance(servers, dict) and name in servers:
                del servers[name]
                atomic_write_json(path, data)
                return True, "local"
    return False, "not found"


def expand_server_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of *entry* with ${VAR} expansion applied to string fields."""
    out = dict(entry)
    for key in ("command", "url", "cwd"):
        if isinstance(out.get(key), str):
            out[key] = expand_env_string(out[key])
    if isinstance(out.get("args"), list):
        out["args"] = [expand_env_string(a) if isinstance(a, str) else a for a in out["args"]]
    if isinstance(out.get("headers"), dict):
        out["headers"] = {
            str(k): expand_env_string(v) if isinstance(v, str) else v
            for k, v in out["headers"].items()
        }
    # env values expanded at spawn time in build_stdio_env; keep templates here.
    return out


def parse_timeout_ms(entry: Mapping[str, Any], default_s: float = 30.0) -> float:
    """Return request timeout in seconds from optional ``timeout`` (ms) field."""
    raw = entry.get("timeout")
    if raw is None:
        return default_s
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return default_s
    if ms < 1000:
        return default_s
    return ms / 1000.0


async def connect_from_entry(
    name: str,
    entry: Mapping[str, Any],
    *,
    project_root: Optional[str | Path] = None,
    client: Optional["MCPClient"] = None,
) -> dict[str, Any]:
    """Connect *client* using a config entry (env/cwd/timeout/headers aware).

    ``client`` is injectable so agent-scoped service containers do not silently
    connect the process-wide singleton. CLI callers omit it and retain the
    process-wide default.
    """
    if client is None:
        from coderAI.tools.mcp import mcp_client

        client = mcp_client

    expanded = expand_server_entry(entry)
    transport = expanded.get("transport", "stdio")
    if transport == "sse":
        return await client.connect_sse(name, expanded.get("url", ""))
    if transport == "http":
        return await client.connect_http(
            name,
            expanded.get("url", ""),
            expanded.get("headers") if isinstance(expanded.get("headers"), dict) else None,
        )
    cwd = resolve_mcp_cwd(expanded.get("cwd"), project_root=project_root)
    env_map = expanded.get("env") if isinstance(expanded.get("env"), dict) else None
    timeout = parse_timeout_ms(expanded, default_s=10.0)
    return await client.connect_stdio(
        name,
        expanded.get("command", ""),
        expanded.get("args") or [],
        env=env_map,
        cwd=cwd,
        timeout=timeout,
    )


def import_mcp_servers_from_file(path: Path) -> dict[str, dict[str, Any]]:
    """Parse Claude Desktop / Cursor / Claude Code / generic mcpServers JSON."""
    data = _load_json_object(path)
    # Claude Desktop: { mcpServers: {...} }
    # Cursor: { mcpServers: {...} } in .cursor/mcp.json
    # Claude Code project: .mcp.json
    # Sometimes nested under mcpServers only
    servers = data.get("mcpServers")
    if servers is None and isinstance(data.get("mcp"), dict):
        # OpenCode-style top-level mcp map
        servers = data["mcp"]
    if not isinstance(servers, dict):
        # Maybe the file IS the servers map
        if all(isinstance(v, dict) for v in data.values()) and data:
            servers = data
        else:
            return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if isinstance(name, str) and isinstance(entry, dict):
            out[name] = normalize_server_entry(entry)
    return out


def find_import_sources() -> list[tuple[str, Path]]:
    """Discover known MCP config files on this machine."""
    home = Path.home()
    candidates: list[tuple[str, Path]] = [
        (
            "claude-desktop",
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        ),
        ("claude-desktop", home / ".config" / "Claude" / "claude_desktop_config.json"),
        ("cursor", home / ".cursor" / "mcp.json"),
        ("claude-code", home / ".claude.json"),
    ]
    found: list[tuple[str, Path]] = []
    for kind, path in candidates:
        if path.is_file():
            found.append((kind, path))
    return found
