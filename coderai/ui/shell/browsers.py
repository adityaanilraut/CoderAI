"""Session, change, and integration browsers built on one focused selector."""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any

from coderai.ui.shell.interaction import BrowserPosition, BrowserRow


def diff_rows(text: str) -> list[BrowserRow]:
    from coderai.utils.rich.diff_render import parse_diff_stats

    added, removed = parse_diff_stats(text)
    rows = [BrowserRow("all", f"All changes: +{added} -{removed}", text)]
    files: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") or (
            line.startswith("--- ") and files and any(item.startswith("@@") for item in files[-1])
        ):
            files.append([])
        if not files:
            files.append([])
        files[-1].append(line)
    for index, lines in enumerate(files):
        name = next(
            (line[4:].strip() for line in lines if line.startswith("+++ ")), f"File {index + 1}"
        )
        body = "".join(lines)
        add, remove = parse_diff_stats(body)
        rows.append(BrowserRow(f"file:{index}", f"{name} +{add} -{remove}", body))
        boundaries = [i for i, line in enumerate(lines) if line.startswith("@@")]
        for n, start in enumerate(boundaries):
            end = boundaries[n + 1] if n + 1 < len(boundaries) else len(lines)
            rows.append(
                BrowserRow(
                    f"hunk:{index}:{n}",
                    f"  {name} {lines[start].strip()}",
                    "".join(lines[: boundaries[0]] + lines[start:end]),
                )
            )
    return rows


async def browse_diff(shell: Any, text: str | None = None) -> None:
    if text is None:
        text = (
            await asyncio.to_thread(shell.mgr.get_diff, shell.view.session_id)
            if shell.view.session_id
            else ""
        )
    if not text:
        shell.presentation.emit("No tracked file changes in this session.")
        return
    rows = diff_rows(text)
    while True:
        choice = await shell.select("Changes (files and hunks)", rows)
        row = next((row for row in rows if row.id == choice.value), None)
        if row is None:
            return
        await shell.inspect(row.label, row.detail)


async def restore_checkpoint(shell: Any) -> None:
    sid = shell.view.session_id
    if not sid:
        shell.presentation.emit("No active session to restore.")
        return
    targets = await asyncio.to_thread(shell.mgr.list_undo_targets, sid)
    selected = await shell.select(
        "Restore checkpoint",
        [
            BrowserRow(
                str(i),
                str(t.get("prompt", "")),
                f"Time: {t.get('create_time', 'unknown')}\nCheckpoint: {t.get('checkpoint_hash') or 'unavailable'}\nFile restore: {'available' if t.get('can_restore_code') else 'unavailable'}",
            )
            for i, t in enumerate(targets)
        ],
    )
    if selected.cancelled:
        return
    target = targets[int(selected.value)]
    messages = await asyncio.to_thread(shell.mgr.list_session_messages, sid)
    cutoff = next((i for i, m in enumerate(messages) if m.id == target.get("message_id")), None)
    removed = messages[cutoff + 1 :] if cutoff is not None else []
    detail = f"Checkpoint: {target.get('create_time', 'unknown')}\nConversation restore removes {len(removed)} messages ({sum(m.role == 'user' for m in removed)} later user turns); retains the selected prompt.\n"
    can_restore = bool(target.get("can_restore_code"))
    diff = ""
    if can_restore:
        try:
            diff = await asyncio.to_thread(
                shell.mgr.get_diff, sid, from_checkpoint=target.get("checkpoint_hash")
            )
        except (OSError, ValueError) as exc:
            can_restore = False
            detail += f"File preview unavailable: {exc}\n"
    detail += "File restoration reverts the following current changes:\n" + (
        diff or "No file changes to preview."
    )
    rows = [BrowserRow("restore_conversation_only", "Restore conversation; keep files", detail)]
    rows += [
        BrowserRow(
            mode,
            label,
            detail if can_restore else "No restorable file checkpoint is available.",
            disabled=not can_restore,
        )
        for mode, label in (
            ("restore_code_only", "Restore files; keep conversation"),
            ("restore_both", "Restore conversation and files"),
        )
    ]
    mode = await shell.select("Choose restoration scope", rows)
    if mode.cancelled:
        return
    confirm = await shell.select(
        "Confirm restoration",
        [
            BrowserRow("cancel", "Keep current state", detail),
            BrowserRow(
                "restore",
                "Restore selected scope: " + str(mode.value).removeprefix("restore_"),
                detail,
            ),
        ],
    )
    if confirm.value != "restore":
        return
    ok = await asyncio.to_thread(
        shell.mgr.undo, sid, target_message_id=target.get("message_id"), mode=str(mode.value)
    )
    shell.presentation.emit("Restored checkpoint." if ok else "Checkpoint restoration failed.")


def session_rows(entries: list, library: dict, args: str) -> tuple[list[BrowserRow], str]:
    import datetime

    flags = library.get("sessions", {})
    filters, words = {}, []
    for part in shlex.split(args):
        key, separator, value = part.partition(":")
        if separator and key in {"status", "after", "before", "archived", "pinned", "fork"}:
            filters[key] = value
        elif part != "tree":
            words.append(part)
    for key in ("after", "before"):
        if key in filters:
            datetime.date.fromisoformat(filters[key])
    by_id = {e.id: e for e in entries}
    children_by_id: dict[str, list[str]] = {}
    for entry in entries:
        if entry.fork_of:
            children_by_id.setdefault(entry.fork_of, []).append(entry.id)
    rows = []
    for entry in entries:
        tag = flags.get(entry.id, {})
        archived = bool(tag.get("archived"))
        archive_filter = filters.get("archived", "false")
        if (archive_filter == "false" and archived) or (
            archive_filter in ("true", "only") and not archived
        ):
            continue
        if "status" in filters and entry.status != filters["status"]:
            continue
        if "pinned" in filters and bool(tag.get("pinned")) != (filters["pinned"] == "true"):
            continue
        date = (entry.update_time or "")[:10]
        if ("after" in filters and date < filters["after"]) or (
            "before" in filters and date > filters["before"]
        ):
            continue
        ancestors, current = [], entry
        while current.fork_of and current.fork_of not in ancestors:
            ancestors.append(current.fork_of)
            if current.fork_of not in by_id:
                break
            current = by_id[current.fork_of]
        if "fork" in filters and not any(
            s.startswith(filters["fork"]) for s in [entry.id, *ancestors]
        ):
            continue
        prefix = ("[pin] " if tag.get("pinned") else "") + ("[archived] " if archived else "")
        tree = (
            "  " * min(len(ancestors), 8) + ("+- " if ancestors else "")
            if "tree" in args.split()
            else ""
        )
        children = children_by_id.get(entry.id, [])
        detail = f"ID: {entry.id}\nStatus: {entry.status}\nUpdated: {entry.update_time}\nParent: {entry.fork_of or 'none'}\nChildren: {', '.join(children) or 'none'}\n/sessions fork:{entry.id} tree explores this branch.\n/sessions manage {entry.id} organizes this session.\n{entry.assistant_reply or ''}"
        rows.append(
            BrowserRow(
                entry.id,
                f"{tree}{prefix}{entry.summary} | {entry.update_time} | {entry.status}",
                detail,
            )
        )
    if "tree" in args.split():

        def ancestry(row):
            ids, entry = [row.id], by_id[row.id]
            while entry.fork_of in by_id and entry.fork_of not in ids:
                ids.append(entry.fork_of)
                entry = by_id[entry.fork_of]
            return tuple(reversed(ids))

        rows.sort(key=ancestry)
    else:
        rows.sort(
            key=lambda r: (bool(flags.get(r.id, {}).get("pinned")), by_id[r.id].update_time),
            reverse=True,
        )
    return rows, " ".join(words)


async def browse_sessions(shell: Any, args: str) -> None:
    action, _, target = args.partition(" ")
    if action in {"pin", "unpin", "archive", "unarchive", "manage"}:
        sid = shell.mgr.resolve_session_id(target) if target else shell.view.session_id
        if not sid:
            raise ValueError("Select a session or provide its ID: /sessions manage <id>")
        if action == "manage":
            choice = await shell.select(
                "Organize session",
                [BrowserRow(a, a.capitalize()) for a in ("pin", "unpin", "archive", "unarchive")],
            )
            if choice.cancelled:
                return
            action = str(choice.value)
        flags = shell.storage.library().get("sessions", {}).get(sid, {})
        flags["pinned" if action in ("pin", "unpin") else "archived"] = action in ("pin", "archive")
        await asyncio.to_thread(shell.storage.update_library, "sessions", sid, flags)
        shell.presentation.emit(
            f"Session {sid}: {action}. /sessions archived:only shows archived sessions."
        )
        return
    if args and not any(c in args for c in (" ", ":")) and args != "tree":
        resolved = shell.mgr.resolve_session_id(args)
        if resolved:
            shell.switch(resolved)
            await shell.drain()
            return
    entries = await asyncio.to_thread(shell.mgr.list_sessions)
    rows, query = session_rows(entries, shell.storage.library(), args)
    if args:
        shell.positions[(shell.view.session_id, "Sessions")] = BrowserPosition(query=query)
    selected = await shell.select("Sessions", rows)
    if not selected.cancelled:
        shell.switch(str(selected.value))
        await shell.drain()


async def browse_integrations(shell: Any, *, tools_only: bool = False) -> None:
    from coderai.ui.shell.security import redact_settings

    manager = shell.mgr.mcp_manager
    while True:
        statuses = manager.get_status()
        tools = manager.list_tools()
        rows: list[BrowserRow] = []
        if not tools_only:
            rows.extend(
                BrowserRow(
                    "server:" + s.name,
                    f"{s.name}: {s.status}",
                    f"Tools: {s.tool_count} | Prompts: {s.prompt_count} | Resources: {s.resource_count}\n{getattr(s, 'error', '') or ''}\nEnter for reconnect / authentication.",
                )
                for s in statuses
            )
        rows.extend(
            BrowserRow(
                f"tool:{t.server_name}:{t.original_name}",
                f"{t.server_name}: {t.original_name}",
                json.dumps(redact_settings(t.definition), indent=2, ensure_ascii=False),
            )
            for t in tools
        )
        if not rows:
            shell.presentation.emit(
                "No MCP integrations available. Add one with `coderai mcp add --help`, then /reload or /mcp reconnect <server>. /tools shows local tools."
            )
            return
        choice = await shell.select("MCP integrations", rows)
        row = next((r for r in rows if r.id == choice.value), None)
        if row is None:
            return
        if row.id.startswith("tool:"):
            await shell.inspect(row.label, row.detail)
            continue
        name = row.id.removeprefix("server:")
        action = await shell.select(
            name,
            [
                BrowserRow("reconnect", "Reconnect", row.detail),
                BrowserRow(
                    "login",
                    "Set bearer token",
                    f"Securely save a remote-server token for {name} in this workspace and reconnect.",
                ),
                BrowserRow("tools", "Browse tools", row.detail),
            ],
        )
        if action.value in ("reconnect", "login") and shell.running:
            raise ValueError("Stop the current turn before changing an integration connection.")
        if action.value == "reconnect":
            ok = await manager.reconnect(name)
            shell.mgr._refresh_mcp_tool_definitions()
            shell.presentation.emit(
                "Reconnected." if ok else "Reconnect failed; inspect the server error."
            )
        elif action.value == "login":
            await authenticate_integration(shell, name)
        elif action.value == "tools":
            subset = [r for r in rows if r.id.startswith(f"tool:{name}:")]
            selected = await shell.select(f"{name} tools", subset)
            tool = next((r for r in subset if r.id == selected.value), None)
            if tool:
                await shell.inspect(tool.label, tool.detail)


async def authenticate_integration(shell: Any, name: str) -> None:
    """Store a token reference for the selected runtime server, including project servers."""
    from hashlib import sha256
    import getpass
    from coderai.config import read_project_settings, write_project_settings
    from coderai.mcp_oauth import store_server_token

    config = dict(shell.mgr.mcp_manager.server_configs.get(name, {}))
    if not config.get("url"):
        shell.presentation.emit("Token authentication is available for remote URL servers only.")
        return
    try:
        with shell.presentation.hold(), shell.prompt.session.app.input.cooked_mode():
            token = (
                await asyncio.to_thread(
                    getpass.getpass, f"Bearer token for {name} (empty cancels): "
                )
            ).strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not token:
        return
    # A project override and a workspace/endpoint-scoped token avoid replacing
    # another project's credentials when server names happen to match.
    identity = sha256(f"{shell.mgr.project_root}:{name}:{config['url']}".encode()).hexdigest()
    stored = await asyncio.to_thread(store_server_token, identity, token)
    for key in ("token", "tokenEnv", "tokenFile"):
        config.pop(key, None)
    config.update(auth="oauth", tokenFile=str(stored))
    settings = read_project_settings(shell.mgr.project_root) or {}
    settings.setdefault("mcpServers", {})[name] = config
    await asyncio.to_thread(write_project_settings, settings, shell.mgr.project_root)
    ok = await shell.mgr.mcp_manager.reconnect(name, config)
    shell.mgr._refresh_mcp_tool_definitions()
    shell.presentation.emit(
        "Token saved and server reconnected."
        if ok
        else "Token saved; reconnect failed. Inspect the server error."
    )
