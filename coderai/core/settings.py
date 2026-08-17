"""Settings resolution — port of deepcode core/src/settings.ts.

Layering: user settings (~/.coderai/settings.json) -> project settings
(<root>/.coderai/settings.json) -> process env (CODERAI_*). Project wins over
user; env wins over both.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Literal

from coderai.core.common.model_capabilities import defaults_to_thinking_mode

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CONTEXT_WINDOW = 256 * 1024

PermissionScope = Literal[
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
]

PermissionDefaultMode = Literal["allowAll", "askAll"]

VALID_PERMISSION_SCOPES = {
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

ReasoningEffort = Literal["high", "max"]


def _home() -> pathlib.Path:
    return pathlib.Path.home()


def get_user_settings_path() -> str:
    return str(_home() / ".coderai" / "settings.json")


def get_project_settings_path(project_root: str = ".") -> str:
    root = pathlib.Path(project_root)
    alt = root / ".coderAI" / "settings.json"
    if alt.is_file():
        return str(alt)
    return str(root / ".coderai" / "settings.json")


def _read_settings_file(path: str) -> dict | None:
    try:
        p = pathlib.Path(path)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_settings() -> dict | None:
    return _read_settings_file(get_user_settings_path())


def read_project_settings(project_root: str = ".") -> dict | None:
    root = pathlib.Path(project_root)
    primary = _read_settings_file(str(root / ".coderai" / "settings.json"))
    alt = _read_settings_file(str(root / ".coderAI" / "settings.json"))
    if primary and alt:
        return {**alt, **primary}
    return primary or alt


def _write_settings_file(path: str, settings: dict) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def write_settings(settings: dict) -> None:
    _write_settings_file(get_user_settings_path(), settings)


def write_project_settings(settings: dict, project_root: str = ".") -> None:
    _write_settings_file(get_project_settings_path(project_root), settings)


def load_dotenv(project_root: str = ".") -> dict[str, str]:
    """Parse key=value pairs from .env in project root and ~/.coderai/.env into os.environ."""
    loaded: dict[str, str] = {}
    candidates = [
        _home() / ".coderai" / ".env",
        pathlib.Path(project_root) / ".env",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if len(v) >= 2 and (
                        (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")
                    ):
                        v = v[1:-1]
                    if k:
                        loaded[k] = v
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass
    return loaded


def collect_env(prefix: str = "CODERAI_", env: dict[str, str] | None = None) -> dict[str, str]:
    env = env if env is not None else dict(os.environ)
    result: dict[str, str] = {}
    for key, value in env.items():
        if key.startswith(prefix) and isinstance(value, str) and value:
            stripped = key[len(prefix) :]
            if stripped:
                result[stripped] = value
    return result


def _trim(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        n = value.strip().lower()
        if n in ("1", "true", "enabled", "yes", "on"):
            return True
        if n in ("0", "false", "disabled", "no", "off"):
            return False
    return None


def _parse_temperature(value: Any) -> float | None:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    return raw if 0 <= raw <= 2 else None


def _normalize_permission_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item in VALID_PERMISSION_SCOPES and item not in result:
            result.append(item)
    return result


def _normalize_default_mode(value: Any) -> PermissionDefaultMode:
    return value if value in ("allowAll", "askAll") else "allowAll"


def _normalize_permissions(settings: dict | None) -> dict[str, Any]:
    perms = (settings or {}).get("permissions") or {}
    return {
        "allow": _normalize_permission_list(perms.get("allow")),
        "deny": _normalize_permission_list(perms.get("deny")),
        "ask": _normalize_permission_list(perms.get("ask")),
        "defaultMode": _normalize_default_mode(perms.get("defaultMode")),
    }


def _merge_lists(*lists: list[str] | None) -> list[str]:
    result: list[str] = []
    for lst in lists:
        for item in lst or []:
            if item not in result:
                result.append(item)
    return result


def _merge_permissions(user: dict | None, project: dict | None) -> dict[str, Any]:
    up = _normalize_permissions(user)
    pp = _normalize_permissions(project)
    return {
        "allow": _merge_lists(up["allow"], pp["allow"]),
        "deny": _merge_lists(up["deny"], pp["deny"]),
        "ask": _merge_lists(up["ask"], pp["ask"]),
        "defaultMode": pp["defaultMode"]
        if (project or {}).get("permissions")
        else up["defaultMode"],
    }


def _normalize_env(env: Any) -> dict[str, str]:
    if not isinstance(env, dict):
        return {}
    return {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)}


def resolve_current_settings(project_root: str = ".") -> dict[str, Any]:
    """Resolve user + project + env into a single settings dict."""
    load_dotenv(project_root)
    user = read_settings() or {}
    project = read_project_settings(project_root) or {}

    user_env = _normalize_env(user.get("env"))
    project_env = _normalize_env(project.get("env"))
    system_env = collect_env("CODERAI_")
    env = {**user_env, **project_env, **system_env}

    def first(*values: Any) -> str:
        for v in values:
            s = _trim(v)
            if s:
                return s
        return ""

    model = (
        first(system_env.get("MODEL"))
        or _trim(project.get("model"))
        or first(project_env.get("MODEL"))
        or _trim(user.get("model"))
        or first(user_env.get("MODEL"))
        or DEFAULT_MODEL
    )

    base_url = (
        first(system_env.get("BASE_URL"))
        or _trim(project.get("baseURL"))
        or first(project_env.get("BASE_URL"))
        or _trim(user.get("baseURL"))
        or first(user_env.get("BASE_URL"))
        or DEFAULT_BASE_URL
    )

    api_key = (
        first(system_env.get("API_KEY"))
        or _trim(project.get("apiKey"))
        or first(project_env.get("API_KEY"))
        or _trim(user.get("apiKey"))
        or first(user_env.get("API_KEY"))
        or None
    )

    # OPENAI_* fallback for compat.
    if not api_key and os.getenv("OPENAI_API_KEY"):
        api_key = os.getenv("OPENAI_API_KEY")
    if base_url == DEFAULT_BASE_URL and os.getenv("OPENAI_BASE_URL"):
        base_url = os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL

    context_window = DEFAULT_CONTEXT_WINDOW
    auto_compact_window = max(1, context_window // 2)

    thinking_enabled = (
        _parse_bool(system_env.get("THINKING_ENABLED"))
        if _parse_bool(system_env.get("THINKING_ENABLED")) is not None
        else (
            _parse_bool(project.get("thinkingEnabled"))
            if _parse_bool(project.get("thinkingEnabled")) is not None
            else (
                _parse_bool(user.get("thinkingEnabled"))
                if _parse_bool(user.get("thinkingEnabled")) is not None
                else defaults_to_thinking_mode(model)
            )
        )
    )

    temperature = (
        _parse_temperature(system_env.get("TEMPERATURE"))
        or _parse_temperature(project.get("temperature"))
        or _parse_temperature(user.get("temperature"))
    )

    return {
        "env": env,
        "apiKey": api_key,
        "baseURL": base_url,
        "model": model,
        "contextWindow": context_window,
        "autoCompactWindow": auto_compact_window,
        "temperature": temperature,
        "thinkingEnabled": thinking_enabled,
        "reasoningEffort": "max",
        "debugLogEnabled": bool(_parse_bool(system_env.get("DEBUG_LOG_ENABLED"))),
        "telemetryEnabled": False,
        "notify": first(system_env.get("NOTIFY"), project.get("notify"), user.get("notify"))
        or None,
        "webSearchTool": (
            first(
                system_env.get("WEB_SEARCH_TOOL"),
                project.get("webSearchTool"),
                user.get("webSearchTool"),
            )
            or None
        ),
        "mcpServers": _merge_mcp_servers(user, project, user_env, project_env, system_env),
        "permissions": _merge_permissions(user, project),
        "enabledSkills": _merge_enabled_skills(user, project),
    }


def _normalize_enabled_skills(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, bool] = {}
    for name, enabled in value.items():
        if isinstance(name, str) and name and isinstance(enabled, bool):
            result[name] = enabled
    return result


def _merge_enabled_skills(user: dict | None, project: dict | None) -> dict[str, bool]:
    return {
        **_normalize_enabled_skills((user or {}).get("enabledSkills")),
        **_normalize_enabled_skills((project or {}).get("enabledSkills")),
    }


def _merge_mcp_servers(
    user: dict,
    project: dict,
    user_env: dict[str, str],
    project_env: dict[str, str],
    system_env: dict[str, str],
) -> dict[str, dict] | None:
    user_servers = user.get("mcpServers") or {}
    project_servers = project.get("mcpServers") or {}
    names = set(user_servers) | set(project_servers)
    if not names:
        return None
    merged: dict[str, dict] = {}
    for name in names:
        uc = user_servers.get(name) or {}
        pc = project_servers.get(name) or {}
        command = pc.get("command") or uc.get("command")
        if not command:
            continue
        cfg: dict = {"command": command}
        args = pc.get("args") or uc.get("args")
        if args is not None:
            cfg["args"] = args
        env_cfg = {**(uc.get("env") or {}), **(pc.get("env") or {})}
        env_cfg = {k: v for k, v in env_cfg.items() if isinstance(k, str) and isinstance(v, str)}
        if env_cfg:
            cfg["env"] = env_cfg
        merged[name] = cfg
    return merged or None
