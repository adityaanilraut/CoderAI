"""Side-effect-scoped permissions — port of deepcode core/src/common/permissions.ts.

Maps every tool call to one or more PermissionScope values, evaluates them
against settings (allow/deny/ask lists + defaultMode), and returns a plan of
per-tool decisions plus the subset that must be surfaced to the UI as `ask`.
"""

from __future__ import annotations

import fnmatch
import json
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from coderai.core.common.validate import clean_json_string
from coderai.core.state import get_snippet, is_absolute_file_path, normalize_file_path

# Scopes (matching settings.PermissionScope). "unknown" is a bash-only sentinel.
ASK_SCOPES = {
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
    "unknown",
}

BASH_SIDE_EFFECTS = {
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "unknown",
}

PLAN_MODE_FORCE_ASK_SCOPES: list[str] = [
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "mutate-git-log",
]

Decision = str  # "allow" | "deny" | "ask"


def get_scope_risk_level(scope: str) -> str:
    """Return risk rating: 'low', 'moderate', 'high', 'critical'."""
    if scope in ("read-in-cwd", "query-git-log"):
        return "low"
    if scope in ("read-out-cwd", "write-in-cwd", "mcp"):
        return "moderate"
    if scope in ("write-out-cwd", "delete-in-cwd", "network"):
        return "high"
    if scope in ("delete-out-cwd", "mutate-git-log"):
        return "critical"
    return "moderate"


def get_request_risk_badge(scopes: list[str]) -> tuple[str, str]:
    """Return (label, color_style) for a set of permission scopes."""
    if not scopes:
        return "SAFE", "green"
    risks = [get_scope_risk_level(s) for s in scopes]
    if "critical" in risks:
        return "CRITICAL RISK", "bold white on red"
    if "high" in risks:
        return "HIGH RISK", "bold red"
    if "moderate" in risks:
        return "MODERATE RISK", "bold yellow"
    return "LOW RISK", "bold green"


def _generate_file_diff_preview(
    project_root: str,
    file_path: str,
    old_str: str | None,
    new_str: str | None,
    full_new_content: str | None = None,
) -> str | None:
    """Generate a unified diff preview of proposed file changes before execution."""
    if not file_path:
        return None
    try:
        from coderai.core.common.file_utils import build_diff_preview

        abs_path = (
            pathlib.Path(file_path)
            if pathlib.Path(file_path).is_absolute()
            else (pathlib.Path(project_root) / file_path)
        ).resolve()

        old_content = (
            abs_path.read_text(encoding="utf-8", errors="replace")
            if abs_path.is_file()
            else ""
        )

        if full_new_content is not None:
            new_content = full_new_content
        elif old_str is not None and new_str is not None:
            if old_content and old_str in old_content:
                new_content = old_content.replace(old_str, new_str, 1)
            else:
                new_content = new_str
        else:
            return None

        diff = build_diff_preview(file_path, old_content, new_content)
        return diff if diff.strip() else None
    except Exception:
        return None


@dataclass
class PermissionTicket:
    """Capability escalation grant with optional scoping, expiration, and usage quotas."""

    ticket_id: str = field(default_factory=lambda: f"tkt_{uuid.uuid4().hex[:8]}")
    session_id: str = ""
    tool_name: str = "*"  # specific tool name or "*" for all tools
    scope: str = "*"  # specific scope or "*"
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    max_uses: int | None = None
    use_count: int = 0
    pattern: str | None = None  # glob pattern matching target path or command prefix

    def is_valid(
        self,
        tool_name: str | None = None,
        scope: str | None = None,
        target: str | None = None,
    ) -> bool:
        """Check if this ticket is active and matches requested tool, scope, and target."""
        now = time.time()
        if self.expires_at is not None and now > self.expires_at:
            return False
        if self.max_uses is not None and self.use_count >= self.max_uses:
            return False
        if self.tool_name != "*" and tool_name and self.tool_name != tool_name:
            return False
        if self.scope != "*" and scope and self.scope != scope:
            return False
        if self.pattern and target:
            if not (
                fnmatch.fnmatch(target, self.pattern) or target.startswith(self.pattern.rstrip("*"))
            ):
                return False
        return True

    def consume(
        self,
        tool_name: str | None = None,
        scope: str | None = None,
        target: str | None = None,
    ) -> bool:
        if not self.is_valid(tool_name=tool_name, scope=scope, target=target):
            return False
        self.use_count += 1
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "scope": self.scope,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "use_count": self.use_count,
            "pattern": self.pattern,
        }


class PermissionTicketRegistry:
    """In-memory manager for active permission escalation tickets."""

    def __init__(self) -> None:
        self._tickets: dict[str, PermissionTicket] = {}

    def grant_ticket(self, ticket: PermissionTicket) -> PermissionTicket:
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def request_escalation(
        self,
        session_id: str,
        tool_name: str = "*",
        scope: str = "*",
        duration_seconds: float | None = None,
        max_uses: int | None = None,
        pattern: str | None = None,
    ) -> PermissionTicket:
        now = time.time()
        expires_at = (now + duration_seconds) if duration_seconds is not None else None
        ticket = PermissionTicket(
            session_id=session_id,
            tool_name=tool_name,
            scope=scope,
            granted_at=now,
            expires_at=expires_at,
            max_uses=max_uses,
            pattern=pattern,
        )
        return self.grant_ticket(ticket)

    def check_and_consume(
        self,
        session_id: str,
        tool_name: str,
        scope: str,
        target: str | None = None,
        consume: bool = True,
    ) -> bool:
        for t in list(self._tickets.values()):
            if t.session_id and t.session_id != session_id:
                continue
            if t.is_valid(tool_name=tool_name, scope=scope, target=target):
                if consume:
                    t.consume(tool_name=tool_name, scope=scope, target=target)
                return True
        return False

    def revoke_ticket(self, ticket_id: str) -> bool:
        return bool(self._tickets.pop(ticket_id, None))

    def list_active_tickets(self, session_id: str | None = None) -> list[PermissionTicket]:
        now = time.time()
        res: list[PermissionTicket] = []
        for t in self._tickets.values():
            if session_id and t.session_id and t.session_id != session_id:
                continue
            if (t.expires_at is None or now <= t.expires_at) and (
                t.max_uses is None or t.use_count < t.max_uses
            ):
                res.append(t)
        return res

    def clear_session_tickets(self, session_id: str) -> None:
        to_remove = [tid for tid, t in self._tickets.items() if t.session_id == session_id]
        for tid in to_remove:
            self._tickets.pop(tid, None)


_ticket_registry = PermissionTicketRegistry()


def get_permission_ticket_registry() -> PermissionTicketRegistry:
    return _ticket_registry


def parse_tool_call_for_permissions(tool_call: Any) -> dict[str, Any] | None:
    if not isinstance(tool_call, dict):
        return None
    if not isinstance(tool_call.get("id"), str):
        return None
    func = tool_call.get("function")
    if not isinstance(func, dict) or not isinstance(func.get("name"), str):
        return None
    args = func.get("arguments")
    return {
        "id": tool_call["id"],
        "type": "function",
        "function": {
            "name": func["name"],
            "arguments": args if isinstance(args, str) else "",
        },
    }


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    cleaned = clean_json_string(raw) if isinstance(raw, str) else raw
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def is_path_in_project(project_root: str, file_path: str) -> bool:
    normalized = normalize_file_path(file_path)
    absolute = (
        normalized
        if is_absolute_file_path(normalized)
        else str(pathlib.Path(project_root, normalized).resolve())
    )
    try:
        rel = pathlib.Path(absolute).resolve().relative_to(pathlib.Path(project_root).resolve())
        return not str(rel).startswith("..")
    except ValueError:
        return False


def _in_directories(project_root: str, file_path: str, directories: list[str] | None) -> bool:
    if not directories:
        return False
    normalized = normalize_file_path(file_path)
    absolute = (
        normalized
        if is_absolute_file_path(normalized)
        else str(pathlib.Path(project_root, normalized).resolve())
    )
    for directory in directories:
        try:
            pathlib.Path(absolute).resolve().relative_to(pathlib.Path(directory).resolve())
            return True
        except ValueError:
            continue
    return False


def parse_bash_side_effects(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["unknown"]
    scopes: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in BASH_SIDE_EFFECTS:
            return ["unknown"]
        if item not in scopes:
            scopes.append(item)
    return ["unknown"] if "unknown" in scopes else scopes


def describe_tool_permission_request(
    *,
    session_id: str,
    project_root: str,
    tool_call: dict[str, Any],
    read_permission_exempt_paths: list[str] | None = None,
    resolve_snippet_path: Callable[[str, str], str | None] | None = None,
) -> dict[str, Any]:
    name = tool_call["function"]["name"]
    args = parse_tool_arguments(tool_call["function"]["arguments"])

    if name in ("glob", "Glob", "grep", "Grep"):
        fp = args.get("path") if isinstance(args.get("path"), str) else ""
        scopes = [_read_scope(project_root, fp)] if fp else ["read-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd(name, fp) if fp else name,
            "scopes": scopes,
        }

    if name in ("read", "Read"):
        fp = args.get("file_path") if isinstance(args.get("file_path"), str) else ""
        scopes = (
            []
            if (fp and _in_directories(project_root, fp, read_permission_exempt_paths))
            else ([_read_scope(project_root, fp)] if fp else [])
        )
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd("read", fp),
            "scopes": scopes,
        }

    if name in ("write", "Write"):
        fp = args.get("file_path") if isinstance(args.get("file_path"), str) else ""
        content = args.get("content") if isinstance(args.get("content"), str) else ""
        diff_prev = _generate_file_diff_preview(
            project_root, fp, None, None, full_new_content=content
        )
        res = {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd("write", fp),
            "scopes": [
                ("write-in-cwd" if is_path_in_project(project_root, fp) else "write-out-cwd")
            ]
            if fp
            else [],
        }
        if diff_prev:
            res["diff_preview"] = diff_prev
        return res

    if name in ("edit", "Edit"):
        fp = args.get("file_path") if isinstance(args.get("file_path"), str) else ""
        snippet_id = args.get("snippet_id") if isinstance(args.get("snippet_id"), str) else ""
        old_str = (
            args.get("old_str")
            or args.get("old_string")
            or args.get("search")
            or args.get("targetContent")
        )
        new_str = (
            args.get("new_str")
            or args.get("new_string")
            or args.get("replace")
            or args.get("replacementContent")
        )
        if not fp and snippet_id and resolve_snippet_path:
            fp = resolve_snippet_path(session_id, snippet_id) or ""
        diff_prev = _generate_file_diff_preview(
            project_root,
            fp,
            old_str if isinstance(old_str, str) else None,
            new_str if isinstance(new_str, str) else None,
        )
        res = {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd("edit", fp),
            "scopes": [
                ("write-in-cwd" if is_path_in_project(project_root, fp) else "write-out-cwd")
            ]
            if fp
            else ["write-out-cwd"],
        }
        if diff_prev:
            res["diff_preview"] = diff_prev
        return res

    if name in ("bash", "Bash"):
        command = args.get("command") if isinstance(args.get("command"), str) else "bash"
        description = args.get("description") if isinstance(args.get("description"), str) else ""
        return {
            "toolCallId": tool_call["id"],
            "name": "bash",
            "command": command,
            "description": description,
            "scopes": parse_bash_side_effects(args.get("sideEffects")),
        }

    if name in ("WebSearch", "web_search"):
        query = args.get("query") if isinstance(args.get("query"), str) else "WebSearch"
        return {
            "toolCallId": tool_call["id"],
            "name": "WebSearch",
            "command": query,
            "scopes": ["network"],
        }

    if name in ("WebFetch", "web_fetch"):
        url = args.get("url") if isinstance(args.get("url"), str) else "WebFetch"
        return {
            "toolCallId": tool_call["id"],
            "name": "WebFetch",
            "command": url,
            "scopes": ["network"],
        }

    if name in ("UnderstandImage", "understand_image"):
        image_path = args.get("image_path") if isinstance(args.get("image_path"), str) else ""
        img_scopes: list[str] = ["network"]
        if image_path and not _in_directories(
            project_root, image_path, read_permission_exempt_paths
        ):
            img_scopes.insert(0, _read_scope(project_root, image_path))
        return {
            "toolCallId": tool_call["id"],
            "name": "UnderstandImage",
            "command": f"understand-image {image_path}" if image_path else "understand-image",
            "scopes": img_scopes,
        }

    if name in ("str_replace_editor", "StrReplaceEditor"):
        cmd = args.get("command") if isinstance(args.get("command"), str) else "view"
        fp = args.get("path") if isinstance(args.get("path"), str) else ""
        if cmd == "view":
            scopes = [_read_scope(project_root, fp)] if fp else ["read-in-cwd"]
        else:
            scopes = (
                [("write-in-cwd" if is_path_in_project(project_root, fp) else "write-out-cwd")]
                if fp
                else ["write-in-cwd"]
            )
        diff_prev = None
        if cmd == "create":
            diff_prev = _generate_file_diff_preview(
                project_root, fp, None, None, full_new_content=args.get("file_text", "")
            )
        elif cmd == "str_replace":
            diff_prev = _generate_file_diff_preview(
                project_root, fp, args.get("old_str"), args.get("new_str")
            )
        res = {
            "toolCallId": tool_call["id"],
            "name": "str_replace_editor",
            "command": f"str_replace_editor {cmd} {fp}".strip(),
            "scopes": scopes,
        }
        if diff_prev:
            res["diff_preview"] = diff_prev
        return res

    if name in ("terminal_open", "terminal_send", "terminal_signal", "terminal_close"):
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": name,
            "scopes": ["write-in-cwd"],
        }

    if name in ("terminal_read", "terminal_list"):
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": name,
            "scopes": [],
        }

    if name == "lsp":
        fp = args.get("file_path") if isinstance(args.get("file_path"), str) else ""
        op = args.get("operation") if isinstance(args.get("operation"), str) else "lsp"
        scopes = [_read_scope(project_root, fp)] if fp else ["read-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": "lsp",
            "command": f"lsp {op} {fp}".strip(),
            "scopes": scopes,
        }

    if name in ("schedule_create", "schedule_delete"):
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": name,
            "scopes": ["write-in-cwd"],
        }

    if name == "schedule_list":
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": name,
            "scopes": [],
        }

    if name.startswith("mcp__"):
        return {"toolCallId": tool_call["id"], "name": name, "command": name, "scopes": ["mcp"]}

    if name in ("Task", "task", "subagent", "SubAgent", "subagent_fork", "SubAgentFork"):
        mode = args.get("mode") if isinstance(args.get("mode"), str) else "read_only"
        description = args.get("description") if isinstance(args.get("description"), str) else name
        scopes = [] if mode == "read_only" else ["write-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": description or name,
            "scopes": scopes,
        }

    if name in ("workflow", "Workflow"):
        meta_obj = args.get("meta")
        meta = meta_obj if isinstance(meta_obj, dict) else {}
        wf_name_val = meta.get("name")
        wf_name = wf_name_val if isinstance(wf_name_val, str) else "workflow"
        return {
            "toolCallId": tool_call["id"],
            "name": "workflow",
            "command": f"workflow {wf_name}",
            "scopes": ["write-in-cwd"],
        }

    if name in ("ralph", "Ralph"):
        mode = args.get("mode") if isinstance(args.get("mode"), str) else "general"
        obj_val = args.get("objective")
        obj = obj_val if isinstance(obj_val, str) else "verify"
        scopes = [] if mode == "read_only" else ["write-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": "ralph",
            "command": f"ralph {obj[:30]}",
            "scopes": scopes,
        }

    if name in ("spawn_teammate", "SpawnTeammate"):
        tm_name = args.get("name") if isinstance(args.get("name"), str) else "teammate"
        mode = args.get("mode") if isinstance(args.get("mode"), str) else "general"
        scopes = [] if mode == "read_only" else ["write-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": "spawn_teammate",
            "command": f"spawn_teammate {tm_name}",
            "scopes": scopes,
        }

    if name in ("team_task_create", "team_task_update", "TeamTaskCreate", "TeamTaskUpdate"):
        task_title = args.get("title") or args.get("task_id") or "task"
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": f"{name} {task_title}",
            "scopes": ["write-in-cwd"],
        }

    if name in (
        "team_task_get",
        "team_task_list",
        "wait_agent",
        "TeamTaskGet",
        "TeamTaskList",
        "WaitAgent",
    ):
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": name,
            "scopes": [],
        }

    if name in ("code_mode", "CodeMode", "python_exec"):
        return {
            "toolCallId": tool_call["id"],
            "name": "code_mode",
            "command": "code_mode",
            "scopes": ["write-in-cwd"],
        }

    if name in ("session_query", "SessionQuery", "session_search", "SessionSearch"):
        query_val = args.get("query")
        query = query_val if isinstance(query_val, str) else "session_query"
        return {
            "toolCallId": tool_call["id"],
            "name": "session_query",
            "command": f"session_query {query[:30]}",
            "scopes": ["read-in-cwd"],
        }

    if name in ("pwsh", "PowerShell", "powershell"):
        command = args.get("command") if isinstance(args.get("command"), str) else "pwsh"
        description = args.get("description") if isinstance(args.get("description"), str) else ""
        return {
            "toolCallId": tool_call["id"],
            "name": "pwsh",
            "command": command,
            "description": description,
            "scopes": parse_bash_side_effects(args.get("sideEffects")),
        }

    return {"toolCallId": tool_call["id"], "name": name, "command": name, "scopes": []}


def _read_scope(project_root: str, file_path: str) -> str:
    return "read-in-cwd" if is_path_in_project(project_root, file_path) else "read-out-cwd"


def _path_cmd(tool: str, file_path: str | None) -> str:
    return f"{tool} {file_path}" if file_path else tool


DEFAULT_PERMISSION_SETTINGS = {"allow": [], "deny": [], "ask": [], "defaultMode": "allowAll"}


def evaluate_permission_scopes(
    scopes: list[str],
    settings: dict[str, Any] | None = None,
    force_ask_scopes: list[str] | set[str] | None = None,
) -> Decision:
    settings = settings or DEFAULT_PERMISSION_SETTINGS
    deny = set(settings.get("deny") or [])
    ask = set(settings.get("ask") or [])
    allow = set(settings.get("allow") or [])
    default_mode = settings.get("defaultMode", "allowAll")
    forced = set(force_ask_scopes or [])

    if "unknown" in scopes and default_mode != "allowAll":
        return "ask"
    if not scopes:
        return "allow"
    known = [s for s in scopes if s != "unknown"]
    if any(s in deny for s in known):
        return "deny"
    if any(s in ask for s in known):
        return "ask"
    if any(s in forced for s in known):
        return "ask"
    if all(s in allow for s in known):
        return "allow"
    return "ask" if default_mode == "askAll" else "allow"


def get_scopes_requiring_ask(
    scopes: list[str],
    settings: dict[str, Any] | None = None,
    force_ask_scopes: list[str] | set[str] | None = None,
) -> list[str]:
    settings = settings or DEFAULT_PERMISSION_SETTINGS
    deny = set(settings.get("deny") or [])
    ask = set(settings.get("ask") or [])
    allow = set(settings.get("allow") or [])
    default_mode = settings.get("defaultMode", "allowAll")
    forced = set(force_ask_scopes or [])
    result: list[str] = []
    for scope in scopes:
        if scope == "unknown":
            if default_mode != "allowAll":
                result.append(scope)
            continue
        if scope in deny:
            continue
        if scope in ask:
            result.append(scope)
            continue
        if scope in forced:
            result.append(scope)
            continue
        if scope in allow:
            continue
        if default_mode == "askAll":
            result.append(scope)
    return result


def compute_tool_call_permissions(
    *,
    session_id: str,
    project_root: str,
    tool_calls: list[Any],
    settings: dict[str, Any] | None = None,
    force_ask_scopes: list[str] | set[str] | None = None,
    read_permission_exempt_paths: list[str] | None = None,
    resolve_snippet_path: Callable[[str, str], str | None] | None = None,
    ticket_registry: PermissionTicketRegistry | None = None,
) -> dict[str, Any]:
    """Return {"permissions": [...], "askPermissions": [...]}."""
    settings = settings or DEFAULT_PERMISSION_SETTINGS
    registry = ticket_registry or _ticket_registry
    permissions: list[dict[str, Any]] = []
    ask_permissions: list[dict[str, Any]] = []

    for raw in tool_calls:
        tool_call = parse_tool_call_for_permissions(raw)
        if not tool_call:
            continue
        request = describe_tool_permission_request(
            session_id=session_id,
            project_root=project_root,
            tool_call=tool_call,
            read_permission_exempt_paths=read_permission_exempt_paths,
            resolve_snippet_path=resolve_snippet_path,
        )
        decision = evaluate_permission_scopes(
            request["scopes"], settings, force_ask_scopes=force_ask_scopes
        )
        if decision == "ask":
            ask_scopes = get_scopes_requiring_ask(
                request["scopes"], settings, force_ask_scopes=force_ask_scopes
            )
            target = request.get("command") or request.get("name")
            uncovered_scopes: list[str] = []
            for sc in ask_scopes:
                if registry.check_and_consume(
                    session_id, request["name"], sc, target=target, consume=False
                ):
                    continue
                uncovered_scopes.append(sc)

            if not uncovered_scopes:
                permissions.append({"toolCallId": tool_call["id"], "permission": "allow"})
            else:
                permissions.append({"toolCallId": tool_call["id"], "permission": "ask"})
                ask_permissions.append(
                    {
                        "toolCallId": tool_call["id"],
                        "scopes": uncovered_scopes,
                        "name": request["name"],
                        "command": request["command"],
                        "description": request.get("description", ""),
                        "diff_preview": request.get("diff_preview"),
                        "risk_level": get_request_risk_badge(uncovered_scopes)[0],
                    }
                )
        else:
            permissions.append({"toolCallId": tool_call["id"], "permission": decision})

    return {"permissions": permissions, "askPermissions": ask_permissions}


def resolve_tool_call_permission(
    tool_call_id: str,
    permission_overrides: list[dict[str, Any]] | None = None,
    message_permissions: list[dict[str, Any]] | None = None,
) -> Decision:
    for item in permission_overrides or []:
        if item.get("toolCallId") == tool_call_id and item.get("permission") in ("allow", "deny"):
            return item["permission"]
    for item in message_permissions or []:
        if item.get("toolCallId") == tool_call_id and item.get("permission") in (
            "allow",
            "deny",
            "ask",
        ):
            return item["permission"]
    return "allow"


def build_synthetic_tool_execution(tool_call: dict[str, Any], error: str) -> dict[str, Any]:
    result = {"ok": False, "name": tool_call["function"]["name"], "error": error}
    return {
        "toolCallId": tool_call["id"],
        "content": json.dumps(result, indent=2),
        "result": result,
    }


def build_permission_tool_execution(
    tool_call: dict[str, Any],
    permission_overrides: list[dict[str, Any]] | None = None,
    message_permissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    decision = resolve_tool_call_permission(
        tool_call["id"], permission_overrides, message_permissions
    )
    if decision == "allow":
        return None
    if decision == "deny":
        return build_synthetic_tool_execution(
            tool_call,
            "User denied the required permission for this tool call. Do not try to bypass this decision.",
        )
    return build_synthetic_tool_execution(
        tool_call,
        "The user has not authorized this tool call yet. Retry only if the permission is still necessary.",
    )


def normalize_ask_permissions(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("toolCallId"), str) or not isinstance(item.get("name"), str):
            continue
        scopes = [s for s in (item.get("scopes") or []) if s in ASK_SCOPES]
        result.append(
            {
                "toolCallId": item["toolCallId"],
                "scopes": scopes,
                "name": item["name"],
                "command": item["command"]
                if isinstance(item.get("command"), str)
                else item["name"],
                "description": item.get("description")
                if isinstance(item.get("description"), str)
                else "",
            }
        )
    return result or None


def append_project_permission_allows(project_root: str, scopes: list[str] | None) -> None:
    """Persist user-granted "always allow" scopes to the project settings file."""
    valid = [s for s in (scopes or []) if s in VALID_WRITE_SCOPES]
    if not valid:
        return
    from coderai.core.settings import read_project_settings, write_project_settings

    settings = read_project_settings(project_root) or {}
    permissions = dict(settings.get("permissions") or {})
    allow = list(permissions.get("allow") or [])
    changed = False
    for scope in valid:
        if scope not in allow:
            allow.append(scope)
            changed = True
    deny = [s for s in (permissions.get("deny") or []) if s not in valid]
    ask = [s for s in (permissions.get("ask") or []) if s not in valid]
    if (
        not changed
        and permissions.get("allow") is not None
        and len(deny) == len(permissions.get("deny") or [])
        and len(ask) == len(permissions.get("ask") or [])
    ):
        return
    permissions["allow"] = allow
    permissions["deny"] = deny
    permissions["ask"] = ask
    settings["permissions"] = permissions
    write_project_settings(settings, project_root)


VALID_WRITE_SCOPES = {
    "read-in-cwd",
    "read-out-cwd",
    "write-in-cwd",
    "write-out-cwd",
    "delete-in-cwd",
    "delete-out-cwd",
    "query-git-log",
    "mutate-git-log",
    "network",
    "mcp",
}


def resolve_snippet_file_path(session_id: str, snippet_id: str) -> str | None:
    snippet = get_snippet(session_id, snippet_id)
    return snippet.file_path if snippet else None
