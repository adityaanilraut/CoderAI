"""Side-effect-scoped permissions — port of deepcode core/src/common/permissions.ts.

Maps every tool call to one or more PermissionScope values, evaluates them
against settings (allow/deny/ask lists + defaultMode), and returns a plan of
per-tool decisions plus the subset that must be surfaced to the UI as `ask`.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from collections.abc import Callable

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
    try:
        parsed = json.loads(raw)
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
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd("write", fp),
            "scopes": [
                ("write-in-cwd" if is_path_in_project(project_root, fp) else "write-out-cwd")
            ]
            if fp
            else [],
        }

    if name in ("edit", "Edit"):
        fp = args.get("file_path") if isinstance(args.get("file_path"), str) else ""
        if not fp:
            snippet_id = args.get("snippet_id") if isinstance(args.get("snippet_id"), str) else ""
            fp = (
                resolve_snippet_path(session_id, snippet_id)
                if (snippet_id and resolve_snippet_path)
                else ""
            )
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": _path_cmd("edit", fp),
            "scopes": [
                ("write-in-cwd" if is_path_in_project(project_root, fp) else "write-out-cwd")
            ]
            if fp
            else ["write-out-cwd"],
        }

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

    if name == "WebSearch":
        query = args.get("query") if isinstance(args.get("query"), str) else "WebSearch"
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": query,
            "scopes": ["network"],
        }

    if name == "UnderstandImage":
        image_path = args.get("image_path") if isinstance(args.get("image_path"), str) else ""
        img_scopes: list[str] = ["network"]
        if image_path and not _in_directories(
            project_root, image_path, read_permission_exempt_paths
        ):
            img_scopes.insert(0, _read_scope(project_root, image_path))
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": f"understand-image {image_path}" if image_path else "understand-image",
            "scopes": img_scopes,
        }

    if name.startswith("mcp__"):
        return {"toolCallId": tool_call["id"], "name": name, "command": name, "scopes": ["mcp"]}

    if name in ("Task", "task", "subagent", "SubAgent"):
        mode = args.get("mode") if isinstance(args.get("mode"), str) else "read_only"
        description = args.get("description") if isinstance(args.get("description"), str) else name
        scopes = [] if mode == "read_only" else ["write-in-cwd"]
        return {
            "toolCallId": tool_call["id"],
            "name": name,
            "command": description or name,
            "scopes": scopes,
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
) -> dict[str, Any]:
    """Return {"permissions": [...], "askPermissions": [...]}."""
    settings = settings or DEFAULT_PERMISSION_SETTINGS
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
        permissions.append({"toolCallId": tool_call["id"], "permission": decision})
        if decision == "ask":
            ask_scopes = get_scopes_requiring_ask(
                request["scopes"], settings, force_ask_scopes=force_ask_scopes
            )
            ask_permissions.append(
                {
                    "toolCallId": tool_call["id"],
                    "scopes": ask_scopes if ask_scopes else request["scopes"],
                    "name": request["name"],
                    "command": request["command"],
                    "description": request.get("description", ""),
                }
            )
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
