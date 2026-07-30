"""CLI subcommands for managing MCP (Model Context Protocol) servers.

Reads and writes user / project / local scopes:

* user — ``~/.coderAI/mcp_servers.json``
* project — ``.mcp.json`` (Claude/Cursor-compatible)
* local — ``~/.coderAI/mcp_local.json`` keyed by project root

Servers added here become available the next time you start a chat
(``ExecutionLoop._autoconnect_mcp_servers``), subject to workspace trust for
project-scoped entries.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional, cast

import click

from coderAI.cli.utils import Display
from coderAI.tools.mcp import (
    effective_mcp_servers,
    mcp_servers_path,
    validate_remote_mcp_url,
    validate_stdio_launch,
)
from coderAI.tools.mcp_config import (
    MCP_SCOPES,
    connect_from_entry,
    find_import_sources,
    import_mcp_servers_from_file,
    load_project_mcp_servers,
    project_mcp_path,
    remove_mcp_server_from_scope,
    save_mcp_server_to_scope,
    set_project_approval,
)


@click.group(invoke_without_command=True)
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Manage MCP (Model Context Protocol) servers."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _stdio_entry(launcher: str, args: Sequence[str], display: Display) -> dict[str, Any]:
    """Build a validated stdio server entry, exiting with an error if disallowed.

    Defers to ``validate_stdio_launch`` — the same choke point ``connect_stdio``
    uses — rather than re-implementing the launcher check. A local copy drifted:
    it rejected versioned interpreters (``python3.12``) and Windows launchers
    (``node.exe``, backslash paths) that connect accepts, and it never looked at
    the inline-exec flags, so ``mcp add x -- node -e '…'`` was written to disk and
    only failed later at connect time.
    """
    error = validate_stdio_launch(launcher, list(args))
    if error:
        display.print_error(error)
        sys.exit(2)
    return {"command": launcher, "args": list(args)}


def _entry_validation_error(entry: dict[str, Any]) -> Optional[str]:
    """Return why *entry* would be refused at connect time, or ``None`` if fine."""
    transport = entry.get("transport", "stdio")
    if transport in ("sse", "http"):
        return validate_remote_mcp_url(str(entry.get("url") or ""))
    args = entry.get("args")
    return validate_stdio_launch(
        str(entry.get("command") or ""),
        [str(a) for a in args] if isinstance(args, list) else None,
    )


def _parse_headers(headers: Sequence[str], display: Display) -> dict[str, str]:
    """Parse repeated ``--header 'Key: Value'`` flags into a dict."""
    out: dict[str, str] = {}
    for raw in headers:
        if ":" not in raw:
            display.print_error(f"Invalid --header {raw!r}; expected 'Key: Value'.")
            sys.exit(2)
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key:
            display.print_error(f"Invalid --header {raw!r}; empty header name.")
            sys.exit(2)
        out[key] = value.strip()
    return out


def _parse_env_flags(env_list: Sequence[str], display: Display) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in env_list:
        if "=" not in raw:
            display.print_error(f"Invalid --env {raw!r}; expected KEY=VALUE.")
            sys.exit(2)
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            display.print_error(f"Invalid --env {raw!r}; empty key.")
            sys.exit(2)
        out[key] = value
    return out


def _interactive_add(display: Display) -> tuple[str, dict[str, Any], str]:
    """Prompt for a server name, transport, and connection details."""
    name = click.prompt("Server name").strip()
    if not name or "__" in name:
        display.print_error("Server name is required and must not contain '__'.")
        sys.exit(2)
    scope = click.prompt(
        "Scope",
        type=click.Choice(list(MCP_SCOPES)),
        default="user",
    )
    transport = click.prompt(
        "Transport",
        type=click.Choice(["stdio", "sse", "http"]),
        default="stdio",
    )
    if transport in ("sse", "http"):
        url = click.prompt("URL").strip()
        err = validate_remote_mcp_url(url)
        if err:
            display.print_error(err)
            sys.exit(2)
        entry: dict[str, Any] = {"transport": transport, "url": url}
        if transport == "http" and click.confirm("Add Authorization header?", default=False):
            auth = click.prompt("Authorization value (e.g. Bearer …)").strip()
            if auth:
                entry["headers"] = {"Authorization": auth}
        return name, entry, scope

    command = click.prompt("Command (e.g. npx)").strip()
    args_str = click.prompt("Args (space-separated)", default="").strip()
    args = args_str.split() if args_str else []
    entry = _stdio_entry(command, args, display)
    env_str = click.prompt("Env KEY=VALUE (comma-separated, optional)", default="").strip()
    if env_str:
        env_map: dict[str, str] = {}
        for part in env_str.split(","):
            if "=" in part:
                k, _, v = part.partition("=")
                env_map[k.strip()] = v.strip()
        if env_map:
            entry["env"] = env_map
    cwd = click.prompt("Working directory (optional)", default="").strip()
    if cwd:
        entry["cwd"] = cwd
    return name, entry, scope


@mcp.command("add", context_settings={"ignore_unknown_options": True})
@click.argument("name", required=False)
@click.argument("command_parts", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "sse", "http"]),
    default=None,
    help="Transport type (default: stdio when a command is given; sse/http with --sse/--http).",
)
@click.option(
    "--command",
    "-c",
    help="Launcher for stdio transport (e.g. npx). Alternative to passing it after '--'.",
)
@click.option(
    "--args",
    "args_str",
    default="",
    help="Comma-separated arguments for --command (stdio transport).",
)
@click.option(
    "--sse",
    "sse_url",
    help="SSE endpoint URL — selects SSE transport instead of stdio.",
)
@click.option(
    "--http",
    "http_url",
    help="Streamable HTTP endpoint URL (e.g. https://host/mcp) — selects HTTP transport.",
)
@click.option(
    "--header",
    "-H",
    "header_list",
    multiple=True,
    help="Header 'Key: Value' for HTTP transport. Repeatable.",
)
@click.option(
    "--env",
    "-e",
    "env_list",
    multiple=True,
    help="Environment KEY=VALUE for stdio servers. Repeatable. Supports ${VAR} expansion at connect.",
)
@click.option("--cwd", help="Working directory for stdio servers.")
@click.option("--timeout", type=int, help="Timeout in milliseconds for initialize/tools calls.")
@click.option(
    "--scope",
    "-s",
    type=click.Choice(list(MCP_SCOPES)),
    default="user",
    help="Config scope: user (default), project (.mcp.json), or local.",
)
@click.option(
    "--preset",
    help="Add a curated preset (see `coderAI mcp catalog`). NAME defaults to the preset name.",
)
def mcp_add(
    name: Optional[str],
    command_parts: tuple[str, ...],
    transport: Optional[str],
    command: Optional[str],
    args_str: str,
    sse_url: Optional[str],
    http_url: Optional[str],
    header_list: tuple[str, ...],
    env_list: tuple[str, ...],
    cwd: Optional[str],
    timeout: Optional[int],
    scope: str,
    preset: Optional[str],
) -> None:
    """Add (or overwrite) an MCP server.

    \b
    Interactive (no args):
        coderAI mcp add

    \b
    Preset:
        coderAI mcp add --preset github
        coderAI mcp add mygh --preset github --scope project

    \b
    Explicit:
        coderAI mcp add fetch -- npx -y @modelcontextprotocol/server-fetch
        coderAI mcp add api --http https://host/mcp -H "Authorization: Bearer TOKEN"
        coderAI mcp add gh -e GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN} -- npx -y @modelcontextprotocol/server-github
    """
    from coderAI.cli.utils import display

    entry: dict[str, Any]
    if preset:
        from coderAI.tools.mcp_catalog import get_preset

        try:
            meta = get_preset(preset)
        except KeyError:
            display.print_error(f"Unknown preset {preset!r}. Run `coderAI mcp catalog`.")
            sys.exit(2)
        entry = dict(meta["entry"])
        server_name = name or preset
        if "__" in server_name:
            display.print_error("Server name must not contain '__'.")
            sys.exit(2)
        env_map = _parse_env_flags(env_list, display)
        if env_map:
            entry_env = dict(entry.get("env") or {})
            entry_env.update(env_map)
            entry["env"] = entry_env
        if cwd:
            entry["cwd"] = cwd
        if timeout is not None:
            entry["timeout"] = timeout
        path = save_mcp_server_to_scope(server_name, entry, scope)
        display.print_success(f"Added preset '{preset}' as '{server_name}' ({scope}) → {path}")
        for key in meta.get("required_env") or []:
            display.print_info(f"Requires env var: {key}")
        display.print_info("It will connect on the next `coderAI chat`.")
        return

    # Interactive wizard when nothing was provided.
    if not name and not command_parts and not command and not sse_url and not http_url:
        server_name, entry, scope = _interactive_add(display)
        path = save_mcp_server_to_scope(server_name, entry, scope)
        display.print_success(f"Added MCP server '{server_name}' ({scope}) → {path}")
        display.print_info("It will connect on the next `coderAI chat`.")
        return

    if not name:
        display.print_error("Server NAME is required (or use --preset / interactive `mcp add`).")
        sys.exit(2)

    if "__" in name:
        display.print_error(
            f"Server name must not contain '__' (reserved for MCP tool ids): {name!r}"
        )
        sys.exit(2)

    explicit_targets = [t for t in (command, sse_url, http_url) if t]
    if command_parts and explicit_targets:
        display.print_error(
            "Pass the command/URL after '--' OR use --command/--sse/--http, not both."
        )
        sys.exit(2)
    if len(explicit_targets) > 1:
        display.print_error("Use exactly one of --command (stdio), --sse, or --http.")
        sys.exit(2)

    header_dict = _parse_headers(header_list, display)
    env_map = _parse_env_flags(env_list, display)

    if command_parts:
        effective = transport or "stdio"
        if effective in ("sse", "http"):
            if len(command_parts) != 1:
                display.print_error(
                    f"{effective.upper()} transport takes a single URL after '--', got: "
                    f"{' '.join(command_parts)}"
                )
                sys.exit(2)
            entry = {"transport": effective, "url": command_parts[0]}
        else:
            entry = _stdio_entry(command_parts[0], command_parts[1:], display)
    elif http_url:
        if transport and transport != "http":
            display.print_error("--http implies HTTP transport; remove --transport.")
            sys.exit(2)
        entry = {"transport": "http", "url": http_url}
    elif sse_url:
        if transport and transport != "sse":
            display.print_error("--sse implies SSE transport; remove --transport.")
            sys.exit(2)
        entry = {"transport": "sse", "url": sse_url}
    elif command:
        if transport and transport != "stdio":
            display.print_error("--command implies stdio transport; remove --transport.")
            sys.exit(2)
        args = [a.strip() for a in args_str.split(",") if a.strip()]
        entry = _stdio_entry(command, args, display)
    else:
        display.print_error(
            "Provide a command after '--' (e.g. -- npx -y @scope/server), "
            "or use --command <launcher> / --sse <url> / --http <url>, "
            "or run `coderAI mcp add` interactively / with --preset."
        )
        sys.exit(2)

    if header_dict:
        if entry.get("transport") != "http":
            display.print_error("--header is only supported for the 'http' transport.")
            sys.exit(2)
        entry["headers"] = header_dict

    if env_map:
        if entry.get("transport", "stdio") in ("sse", "http"):
            display.print_error("--env is only supported for stdio servers.")
            sys.exit(2)
        entry["env"] = env_map
    if cwd:
        if entry.get("transport", "stdio") in ("sse", "http"):
            display.print_error("--cwd is only supported for stdio servers.")
            sys.exit(2)
        entry["cwd"] = cwd
    if timeout is not None:
        entry["timeout"] = timeout

    if entry.get("transport") in ("sse", "http"):
        scheme_err = validate_remote_mcp_url(cast(str, entry.get("url", "")))
        if scheme_err:
            display.print_error(scheme_err)
            sys.exit(2)

    # Detect overwrite in the target scope before writing.
    from coderAI.tools.mcp import load_mcp_servers
    from coderAI.tools.mcp_config import load_local_mcp_servers, load_project_mcp_servers

    prior: dict[str, Any] = {}
    if scope == "user":
        prior = load_mcp_servers().get("mcpServers", {}) or {}
    elif scope == "project":
        prior = load_project_mcp_servers()
    else:
        prior = load_local_mcp_servers()
    overwriting = name in prior

    path = save_mcp_server_to_scope(name, entry, scope)
    if overwriting:
        display.print_warning(f"Overwriting existing MCP server '{name}'")
    display.print_success(f"Added MCP server '{name}' ({scope}) → {path}")
    display.print_info("It will connect on the next `coderAI chat`.")


@mcp.command("list")
def mcp_list() -> None:
    """List configured MCP servers with scope and approval status."""
    from coderAI.cli.utils import display
    from coderAI.tools.mcp_oauth import has_credentials

    servers = effective_mcp_servers().get("mcpServers", {})
    if not servers:
        display.print_info(f"No MCP servers configured ({mcp_servers_path()}).")
        return

    rows = []
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        if transport in ("sse", "http"):
            target = cfg.get("url", "")
            args = ""
        else:
            target = cfg.get("command", "")
            args = " ".join(cfg.get("args", []) or [])
        if transport == "http":
            auth = "logged in" if has_credentials(name) else "—"
        else:
            auth = "n/a"
        status = "disabled" if cfg.get("disabled") else "ready"
        if cfg.get("_connect_blocked"):
            status = str(cfg["_connect_blocked"])
        elif cfg.get("bundled"):
            status = "bundled" if status == "ready" else status
        rows.append(
            {
                "Name": name,
                "Scope": cfg.get("_scope", "user"),
                "Transport": transport,
                "Command/URL": target,
                "Args": args,
                "Status": status,
                "Auth": auth,
            }
        )

    display.print_table(rows, "Configured MCP servers")
    # Also print plain names so truncated Rich columns still leave a searchable trail
    # (and so tests / scripts can grep reliably).
    names = ", ".join(str(r["Name"]) for r in rows)
    display.print_info(f"Servers: {names}")
    display.print_info(
        "Project servers stay pending until `coderAI mcp approve <name>` "
        "(after trusting the workspace)."
    )


@mcp.command("get")
@click.argument("name")
def mcp_get(name: str) -> None:
    """Show the resolved config entry for NAME."""
    from coderAI.cli.utils import display

    entry = effective_mcp_servers().get("mcpServers", {}).get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    # Strip internal keys for display but keep them visible under _meta.
    public = {k: v for k, v in entry.items() if not str(k).startswith("_")}
    meta = {k: v for k, v in entry.items() if str(k).startswith("_")}
    display.print(json.dumps({"server": name, "config": public, "meta": meta}, indent=2))


@mcp.command("remove")
@click.argument("name")
@click.option(
    "--scope",
    "-s",
    type=click.Choice(list(MCP_SCOPES)),
    default=None,
    help="Remove from this scope only (default: first match local→project→user).",
)
def mcp_remove(name: str, scope: Optional[str]) -> None:
    """Remove the MCP server named NAME."""
    from coderAI.cli.utils import display

    removed, where = remove_mcp_server_from_scope(name, scope)
    if not removed:
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    from coderAI.tools.mcp_oauth import delete_credentials

    delete_credentials(name)
    display.print_success(f"Removed MCP server '{name}' from {where} scope.")


@mcp.command("enable")
@click.argument("name")
def mcp_enable(name: str) -> None:
    """Enable a previously disabled MCP server, in whichever scope defines it."""
    from coderAI.cli.utils import display
    from coderAI.tools.mcp import set_mcp_server_disabled

    if not set_mcp_server_disabled(name, False):
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    display.print_success(f"Enabled MCP server '{name}'.")


@mcp.command("disable")
@click.argument("name")
def mcp_disable(name: str) -> None:
    """Disable an MCP server so it does not autoconnect.

    The flag is written to the scope that defines the server: local entries are
    edited in place, project ``.mcp.json`` servers are marked rejected in the
    per-user approval store, and user/bundled servers use ``mcp_servers.json``.
    """
    from coderAI.cli.utils import display
    from coderAI.tools.mcp import set_mcp_server_disabled

    if not set_mcp_server_disabled(name, True):
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)
    display.print_success(f"Disabled MCP server '{name}'.")


@mcp.command("approve")
@click.argument("name")
def mcp_approve(name: str) -> None:
    """Approve a pending project-scoped MCP server from ``.mcp.json``."""
    from coderAI.cli.utils import display

    entry = load_project_mcp_servers().get(name)
    if not entry:
        display.print_error(f"No project MCP server named '{name}' in {project_mcp_path()}.")
        sys.exit(1)
    set_project_approval(name, approved=True, entry=entry)
    display.print_success(f"Approved project MCP server '{name}'.")


@mcp.command("reject")
@click.argument("name")
def mcp_reject(name: str) -> None:
    """Reject a project-scoped MCP server from ``.mcp.json``."""
    from coderAI.cli.utils import display

    entry = load_project_mcp_servers().get(name) or {}
    set_project_approval(name, approved=False, entry=entry)
    display.print_success(f"Rejected project MCP server '{name}'.")


@mcp.command("catalog")
def mcp_catalog() -> None:
    """List curated MCP server presets."""
    from coderAI.cli.utils import display
    from coderAI.tools.mcp_catalog import list_presets

    rows = [
        {
            "Preset": p["name"],
            "Transport": p["transport"],
            "Required env": ", ".join(p["required_env"]) or "—",
            "Description": p["description"][:70],
        }
        for p in list_presets()
    ]
    display.print_table(rows, "MCP presets (`coderAI mcp add --preset NAME`)")


@mcp.command("import")
@click.option(
    "--from",
    "source",
    type=click.Choice(["claude-desktop", "cursor", "claude-code", "file"]),
    required=True,
    help="Import source.",
)
@click.option(
    "--path", "file_path", type=click.Path(exists=True, dir_okay=False), help="For --from file."
)
@click.option(
    "--scope",
    "-s",
    type=click.Choice(list(MCP_SCOPES)),
    default="user",
    help="Scope to write imported servers into.",
)
@click.option("--name", "only_name", help="Import only this server name.")
def mcp_import(source: str, file_path: Optional[str], scope: str, only_name: Optional[str]) -> None:
    """Import MCP servers from Claude Desktop, Cursor, Claude Code, or a JSON file."""
    from coderAI.cli.utils import display

    path: Optional[Path] = Path(file_path) if file_path else None
    if source == "file":
        if path is None:
            display.print_error("--path is required with --from file.")
            sys.exit(2)
    else:
        found = {kind: p for kind, p in find_import_sources() if kind == source}
        if path is None:
            if source not in found:
                display.print_error(f"Could not find a {source} config on this machine.")
                sys.exit(1)
            path = found[source]

    assert path is not None
    imported = import_mcp_servers_from_file(path)
    if only_name:
        if only_name not in imported:
            display.print_error(f"No server named '{only_name}' in {path}.")
            sys.exit(1)
        imported = {only_name: imported[only_name]}
    if not imported:
        display.print_info(f"No MCP servers found in {path}.")
        return

    count = 0
    for srv_name, entry in imported.items():
        if "__" in srv_name:
            display.print_warning(f"Skipping invalid name {srv_name!r} (contains '__').")
            continue
        # Foreign configs are written for other clients and can name launchers or
        # schemes CoderAI refuses to start. Rejecting them here beats importing a
        # server that silently fails to autoconnect in every later session.
        entry_error = _entry_validation_error(entry)
        if entry_error:
            display.print_warning(f"Skipping '{srv_name}': {entry_error}")
            continue
        save_mcp_server_to_scope(srv_name, entry, scope)
        count += 1
        display.print_success(f"Imported '{srv_name}' → {scope}")
    display.print_info(f"Imported {count} server(s) from {path}.")


@mcp.command("debug")
@click.argument("name")
def mcp_debug(name: str) -> None:
    """Connect once, print initialize/tools diagnostics, then disconnect."""
    import asyncio

    from coderAI.cli.utils import display
    from coderAI.tools.mcp import mcp_client

    entry = effective_mcp_servers().get("mcpServers", {}).get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'.")
        sys.exit(1)

    async def _run() -> dict[str, Any]:
        result = await connect_from_entry(name, entry)
        if not result.get("success"):
            return result
        info = {
            "success": True,
            "connect": result,
            "tools": [t.get("name") for t in mcp_client.servers.get(name, {}).get("tools", [])],
            "resources": len(
                [r for r in mcp_client.discovered_resources if r.get("server") == name]
            ),
            "prompts": len([p for p in mcp_client.discovered_prompts if p.get("server") == name]),
            "server_info": result.get("server_info"),
        }
        await mcp_client.disconnect(name)
        return info

    out = asyncio.run(_run())
    if not out.get("success"):
        display.print_error(f"Debug connect failed: {out.get('error')}")
        sys.exit(1)
    display.print(json.dumps(out, indent=2, default=str))


@mcp.command("login")
@click.argument("name")
@click.option("--client-id", help="Pre-issued OAuth client id (for servers without registration).")
@click.option("--client-secret", help="OAuth client secret, if the client is confidential.")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="OAuth scope to request (repeatable). Defaults to the server's advertised scopes.",
)
def mcp_login(
    name: str,
    client_id: Optional[str],
    client_secret: Optional[str],
    scopes: tuple[str, ...],
) -> None:
    """Authorize an HTTP MCP server via OAuth and save the credentials."""
    from coderAI.cli.utils import display
    from coderAI.tools import mcp_oauth

    entry = effective_mcp_servers().get("mcpServers", {}).get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'. Add it first with `mcp add`.")
        sys.exit(1)
    if entry.get("transport") != "http":
        display.print_error(
            f"OAuth login only applies to HTTP MCP servers; '{name}' uses "
            f"{entry.get('transport', 'stdio')!r}."
        )
        sys.exit(2)
    url = entry.get("url")
    if not url:
        display.print_error(f"Server '{name}' has no url configured.")
        sys.exit(2)

    display.print_info(f"Discovering authorization server for '{name}' …")
    try:
        record = mcp_oauth.login(
            name,
            url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=list(scopes) or None,
        )
    except mcp_oauth.OAuthError as e:
        display.print_error(f"Login failed: {e}")
        sys.exit(1)
    except Exception as e:
        display.print_error(f"Login failed: {e}")
        sys.exit(1)

    scope_note = f" (scopes: {record['scope']})" if record.get("scope") else ""
    display.print_success(f"Authorized '{name}'{scope_note}.")
    display.print_info("It will connect automatically on the next `coderAI chat`.")


@mcp.command("logout")
@click.argument("name")
def mcp_logout(name: str) -> None:
    """Revoke and delete saved OAuth credentials for an MCP server."""
    from coderAI.cli.utils import display
    from coderAI.tools.mcp_oauth import logout

    if logout(name):
        display.print_success(f"Logged out of MCP server '{name}'.")
    else:
        display.print_info(f"No saved credentials for '{name}'.")


async def _connect_and_list(name: str, entry: dict[str, Any], kind: str) -> dict[str, Any]:
    """Connect transiently to a configured server, list resources/prompts, disconnect."""
    from coderAI.tools.mcp import mcp_client

    conn = await connect_from_entry(name, entry)
    if not conn.get("success"):
        return conn
    try:
        if kind == "resources":
            return await mcp_client.list_resources(name)
        return await mcp_client.list_prompts(name)
    finally:
        await mcp_client.disconnect(name)


def _require_server(name: str, display: Display) -> dict[str, Any]:
    """Return the configured (or bundled) entry for NAME or exit with an error."""
    entry = effective_mcp_servers().get("mcpServers", {}).get(name)
    if not entry:
        display.print_error(f"No MCP server named '{name}'. Add it first with `mcp add`.")
        sys.exit(1)
    return cast("dict[str, Any]", entry)


@mcp.command("resources")
@click.argument("name")
def mcp_resources(name: str) -> None:
    """List resources exposed by the MCP server named NAME."""
    import asyncio

    from coderAI.cli.utils import display

    entry = _require_server(name, display)
    result = asyncio.run(_connect_and_list(name, entry, "resources"))
    if not result.get("success"):
        display.print_error(f"Could not list resources for '{name}': {result.get('error')}")
        sys.exit(1)

    resources = result.get("resources", [])
    if not resources:
        display.print_info(f"'{name}' exposes no resources.")
        return
    rows = [
        {
            "URI": r.get("uri", ""),
            "Name": r.get("name", ""),
            "Type": r.get("mimeType", ""),
            "Description": (r.get("description", "") or "")[:60],
        }
        for r in resources
    ]
    display.print_table(rows, f"Resources on '{name}'")


@mcp.command("prompts")
@click.argument("name")
def mcp_prompts(name: str) -> None:
    """List prompt templates exposed by the MCP server named NAME."""
    import asyncio

    from coderAI.cli.utils import display

    entry = _require_server(name, display)
    result = asyncio.run(_connect_and_list(name, entry, "prompts"))
    if not result.get("success"):
        display.print_error(f"Could not list prompts for '{name}': {result.get('error')}")
        sys.exit(1)

    prompts = result.get("prompts", [])
    if not prompts:
        display.print_info(f"'{name}' exposes no prompts.")
        return
    rows = [
        {
            "Name": p.get("name", ""),
            "Arguments": ", ".join(
                a.get("name", "") for a in (p.get("arguments") or []) if isinstance(a, dict)
            ),
            "Description": (p.get("description", "") or "")[:60],
        }
        for p in prompts
    ]
    display.print_table(rows, f"Prompts on '{name}'")
