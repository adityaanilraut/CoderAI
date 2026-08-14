"""MCP connection and prompt application service for the TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coderAI.application.tui_session_service import _cmd_send_message

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge


async def _cmd_list_mcp_servers(server: UIBridge, _msg: dict[str, Any]) -> None:
    """Emit the merged live + configured MCP server list for the /mcp picker."""
    from coderAI.tools.mcp import effective_mcp_servers, mcp_client

    root = getattr(getattr(server, "agent", None), "config", None)
    project_root = getattr(root, "project_root", ".") if root else "."
    trusted = bool(getattr(getattr(server, "agent", None), "_workspace_trusted", False))
    configured = effective_mcp_servers(project_root=project_root, workspace_trusted=trusted).get(
        "mcpServers", {}
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, info in mcp_client.servers.items():
        seen.add(name)
        cfg = configured.get(name, {})
        rows.append(
            {
                "name": name,
                "connected": True,
                "disabled": bool(cfg.get("disabled")),
                "degraded": bool(info.get("degraded")),
                "tools": len(info.get("tools", [])),
                "transport": info.get("transport", "stdio"),
                "scope": cfg.get("_scope", "user"),
                "approval": cfg.get("_approval") or cfg.get("_connect_blocked"),
            }
        )
    for name, cfg in configured.items():
        if name in seen:
            continue
        rows.append(
            {
                "name": name,
                "connected": False,
                "disabled": bool(cfg.get("disabled")),
                "degraded": False,
                "tools": 0,
                "transport": cfg.get("transport", "stdio"),
                "scope": cfg.get("_scope", "user"),
                "approval": cfg.get("_approval") or cfg.get("_connect_blocked"),
            }
        )

    rows.sort(key=lambda r: str(r["name"]))
    server.emit("available_mcp_servers", servers=rows)
    if not rows:
        server.emit(
            "info",
            message="No MCP servers configured. Add one with `coderAI mcp add`.",
        )


async def _cmd_toggle_mcp_server(server: UIBridge, msg: dict[str, Any]) -> None:
    """Toggle an MCP server on/off — persistent (config) + live (connection).

    Off: disconnect the live connection now and mark it ``disabled`` so it does
    not auto-reconnect next session. On: connect now and clear the flag. Pending
    project servers are approved on toggle-on.
    """
    from coderAI.tools.mcp import effective_mcp_servers, mcp_client, set_mcp_server_disabled
    from coderAI.tools.mcp_config import (
        connect_from_entry,
        load_project_mcp_servers,
        set_project_approval,
    )

    name = str(msg.get("server", "")).strip()
    if not name:
        server.emit("warning", message="Usage: /mcp <server-name>")
        return

    root = getattr(getattr(server, "agent", None), "config", None)
    project_root = getattr(root, "project_root", ".") if root else "."
    trusted = bool(getattr(getattr(server, "agent", None), "_workspace_trusted", False))

    if name in mcp_client.servers:
        await mcp_client.disconnect(name)
        persisted = set_mcp_server_disabled(name, True, project_root=project_root)
        if persisted:
            server.emit("success", message=f"MCP server '{name}' turned off (disconnected)")
        else:
            server.emit(
                "warning",
                message=(
                    f"MCP server '{name}' disconnected, but the off state could not be "
                    "saved — it will reconnect on the next start."
                ),
            )
        await _cmd_list_mcp_servers(server, {})
        return

    cfg = (
        effective_mcp_servers(project_root=project_root, workspace_trusted=trusted)
        .get("mcpServers", {})
        .get(name)
    )
    if not isinstance(cfg, dict):
        server.emit("warning", message=f"No MCP server named '{name}' is configured.")
        return

    # Approving a pending project server via /mcp toggle.
    if cfg.get("_scope") == "project" and cfg.get("_connect_blocked") in ("pending", "rejected"):
        project_entry = load_project_mcp_servers(project_root).get(name) or cfg
        set_project_approval(name, approved=True, entry=project_entry, project_root=project_root)
        cfg = (
            effective_mcp_servers(project_root=project_root, workspace_trusted=trusted)
            .get("mcpServers", {})
            .get(name, cfg)
        )

    result = await connect_from_entry(name, cfg, project_root=project_root)

    if result.get("success"):
        set_mcp_server_disabled(name, False, project_root=project_root)
        count = result.get("tools_discovered", 0)
        server.emit("success", message=f"MCP server '{name}' turned on ({count} tools)")
    else:
        server.emit("warning", message=f"Failed to connect '{name}': {result.get('error')}")
    await _cmd_list_mcp_servers(server, {})


async def _cmd_invoke_mcp_prompt(server: UIBridge, msg: dict[str, Any]) -> None:
    """Fetch an MCP prompt template and inject it as the next user message."""
    from coderAI.tools.mcp import mcp_client

    srv = str(msg.get("server", "")).strip()
    prompt = str(msg.get("prompt", "")).strip()
    arguments = msg.get("arguments") if isinstance(msg.get("arguments"), dict) else {}
    if not srv or not prompt:
        server.emit("warning", message="Usage: /mcp__<server>__<prompt> [key=value…]")
        return
    if srv not in mcp_client.servers:
        server.emit(
            "warning",
            message=f"MCP server '{srv}' is not connected. Use /mcp {srv} to enable it.",
        )
        return
    result = await mcp_client.get_prompt(srv, prompt, arguments)
    if not result.get("success"):
        server.emit("warning", message=f"MCP prompt failed: {result.get('error')}")
        return
    messages = result.get("messages") or []
    chunks: list[str] = []
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "user")
            content = m.get("content")
            if isinstance(content, list):
                text = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            elif isinstance(content, dict):
                text = str(content.get("text", content))
            else:
                text = str(content or "")
            chunks.append(f"[{role}] {text}".strip())
    description = result.get("description") or ""
    body = "\n\n".join(c for c in chunks if c) or description or f"(empty prompt {prompt})"
    server.emit("info", message=f"Loaded MCP prompt mcp__{srv}__{prompt}")
    await _cmd_send_message(server, {"text": body})
