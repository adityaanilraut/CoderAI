"""Settings resolution — port of deepcode core/src/settings.ts.

Layering: user settings (~/.coderai/settings.json) -> project settings
(<root>/.coderai/settings.json) -> process env (CODERAI_*). Project wins over
user; env wins over both.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any, Literal

from coderai.core.common.model_capabilities import defaults_to_thinking_mode
from coderai.core.sandbox import apply_preset, parse_sandbox_mode

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

ReasoningEffort = Literal["off", "low", "medium", "high", "max"]
DEFAULT_REASONING_EFFORT: ReasoningEffort = "max"
VALID_REASONING_EFFORTS = {"off", "low", "medium", "high", "max"}
_REASONING_EFFORT_ALIASES = {
    "none": "off",
    "disabled": "off",
    "false": "off",
    "0": "off",
}


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


def parse_reasoning_effort(value: Any) -> ReasoningEffort | None:
    """Normalize a reasoning-effort setting to off|low|medium|high|max."""
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    normalized = _REASONING_EFFORT_ALIASES.get(raw, raw)
    if normalized in VALID_REASONING_EFFORTS:
        return normalized  # type: ignore[return-value]
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


def parse_token_window(value: Any) -> int | None:
    """Parse numeric token window or shorthand string like '128k', '1m', '256000'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        val = int(value)
        return val if val > 0 else None
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    if s.isdigit():
        val = int(s)
        return val if val > 0 else None
    m = re.match(r"^(\d+)\s*([km])$", s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        multiplier = (1024 * 1024) if unit == "m" else 1024
        res = amount * multiplier
        return res if res > 0 else None
    return None


def first_token_window(*values: Any) -> int | None:
    for v in values:
        parsed = parse_token_window(v)
        if parsed is not None:
            return parsed
    return None


def get_default_context_window(model: str = "") -> int:
    m = (model or "").strip().lower()
    if "deepseek" in m or "v4" in m or "r1" in m or "v3" in m:
        return 1024 * 1024
    if any(k in m for k in ("gpt-5", "gpt-4.5", "claude-3-7", "gemini-2.5", "gemini-2.0")):
        return 512 * 1024
    return DEFAULT_CONTEXT_WINDOW


def get_default_auto_compact_window(model: str = "") -> int:
    return max(1, get_default_context_window(model) // 2)


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

    configured_context_window = first_token_window(
        system_env.get("CONTEXT_WINDOW"),
        project.get("contextWindow"),
        user.get("contextWindow"),
    )
    context_window = configured_context_window or get_default_context_window(model)

    configured_auto_compact_window = first_token_window(
        system_env.get("AUTO_COMPACT_WINDOW"),
        project.get("autoCompactWindow"),
        user.get("autoCompactWindow"),
    )
    default_auto_compact_window = max(1, context_window // 2)
    auto_compact_window = min(
        configured_auto_compact_window or default_auto_compact_window,
        context_window,
    )

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

    multimodal = (
        _resolve_multimodal_mode(system_env.get("MULTIMODAL"))
        or _resolve_multimodal_mode(project.get("multimodal"))
        or _resolve_multimodal_mode(user.get("multimodal"))
        or "default"
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
        "reasoningEffort": (
            parse_reasoning_effort(system_env.get("CODERAI_REASONING_EFFORT"))
            or parse_reasoning_effort(system_env.get("REASONING_EFFORT"))
            or parse_reasoning_effort(project.get("reasoningEffort"))
            or parse_reasoning_effort(project_env.get("CODERAI_REASONING_EFFORT"))
            or parse_reasoning_effort(project_env.get("REASONING_EFFORT"))
            or parse_reasoning_effort(user.get("reasoningEffort"))
            or parse_reasoning_effort(user_env.get("CODERAI_REASONING_EFFORT"))
            or parse_reasoning_effort(user_env.get("REASONING_EFFORT"))
            or DEFAULT_REASONING_EFFORT
        ),
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
        "multimodal": multimodal,
        "mcpServers": _merge_mcp_servers(user, project, user_env, project_env, system_env),
        "permissions": apply_preset(
            _merge_permissions(user, project),
            parse_sandbox_mode(
                first(
                    system_env.get("PERMISSION_PRESET"),
                    project.get("permissionPreset"),
                    user.get("permissionPreset"),
                    ((project.get("permissions") or {}).get("preset")),
                    ((user.get("permissions") or {}).get("preset")),
                )
            ),
        ),
        "enabledSkills": _merge_enabled_skills(user, project),
        "skillScanPaths": _merge_skill_scan_paths(user, project),
        "statusline": _merge_statusline(user, project),
    }


def _resolve_multimodal_mode(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    v = val.strip().lower()
    if v in ("on", "true", "yes", "1"):
        return "on"
    if v in ("off", "false", "no", "0"):
        return "off"
    if v == "default":
        return "default"
    return None


def _normalize_skill_scan_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(p).strip() for p in value if isinstance(p, (str, pathlib.Path)) and str(p).strip()]


def _merge_skill_scan_paths(user: dict | None, project: dict | None) -> list[str]:
    u_paths = _normalize_skill_scan_paths((user or {}).get("skillScanPaths"))
    p_paths = _normalize_skill_scan_paths((project or {}).get("skillScanPaths"))
    combined: list[str] = []
    for p in u_paths + p_paths:
        if p not in combined:
            combined.append(p)
    return combined


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


def _merge_string_map(*maps: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for item in maps:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(key, str) and isinstance(value, str):
                merged[key] = value
    return merged


def _merge_mcp_server_config(user_cfg: Any, project_cfg: Any) -> dict[str, Any] | None:
    """Merge one MCP server. Keep stdio (`command`) and SSE (`url`) configs."""
    uc = user_cfg if isinstance(user_cfg, dict) else {}
    pc = project_cfg if isinstance(project_cfg, dict) else {}

    command = _trim(pc.get("command")) or _trim(uc.get("command"))
    url = _trim(pc.get("url")) or _trim(uc.get("url"))
    if not command and not url:
        return None

    cfg: dict[str, Any] = {}
    if command:
        cfg["command"] = command
    if url:
        cfg["url"] = url

    args = pc.get("args") if "args" in pc else uc.get("args")
    if args is not None:
        cfg["args"] = args

    cwd = _trim(pc.get("cwd")) or _trim(uc.get("cwd"))
    if cwd:
        cfg["cwd"] = cwd

    env_cfg = _merge_string_map(uc.get("env"), pc.get("env"))
    if env_cfg:
        cfg["env"] = env_cfg

    headers = _merge_string_map(uc.get("headers"), pc.get("headers"))
    if headers:
        cfg["headers"] = headers

    if "disabled" in pc:
        cfg["disabled"] = bool(pc["disabled"])
    elif "disabled" in uc:
        cfg["disabled"] = bool(uc["disabled"])

    if "enabled" in pc:
        cfg["enabled"] = bool(pc["enabled"])
    elif "enabled" in uc:
        cfg["enabled"] = bool(uc["enabled"])

    if "allowPrivateIps" in pc:
        cfg["allowPrivateIps"] = bool(pc["allowPrivateIps"])
    elif "allowPrivateIps" in uc:
        cfg["allowPrivateIps"] = bool(uc["allowPrivateIps"])

    return cfg


def _merge_mcp_servers(
    user: dict,
    project: dict,
    _user_env: dict[str, str],
    _project_env: dict[str, str],
    _system_env: dict[str, str],
) -> dict[str, dict] | None:
    user_servers = user.get("mcpServers") or {}
    project_servers = project.get("mcpServers") or {}
    if not isinstance(user_servers, dict):
        user_servers = {}
    if not isinstance(project_servers, dict):
        project_servers = {}
    names = set(user_servers) | set(project_servers)
    if not names:
        return None
    merged: dict[str, dict] = {}
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        cfg = _merge_mcp_server_config(user_servers.get(name), project_servers.get(name))
        if cfg:
            merged[name] = cfg
    return merged or None


DEFAULT_STATUSLINE_REFRESH_MS = 3000
DEFAULT_STATUSLINE_SEPARATOR = " | "


def _normalize_statusline_provider(provider: Any) -> dict[str, Any] | None:
    if not isinstance(provider, dict):
        return None
    ptype = provider.get("type")
    if ptype not in ("command", "module"):
        return None
    result: dict[str, Any] = {"type": ptype}
    for key in ("id", "command", "path", "cwd", "color"):
        val = provider.get(key)
        if isinstance(val, str) and val.strip():
            result[key] = val.strip()
    if ptype == "command" and "command" not in result:
        return None
    if ptype == "module" and "path" not in result:
        return None

    if "timeoutMs" in provider:
        try:
            t = int(provider["timeoutMs"])
            if t > 0:
                result["timeoutMs"] = t
        except (ValueError, TypeError):
            pass

    if "maxLength" in provider:
        try:
            m = int(provider["maxLength"])
            if m > 0:
                result["maxLength"] = m
        except (ValueError, TypeError):
            pass

    if "newLine" in provider:
        result["newLine"] = bool(provider["newLine"])

    return result


def _normalize_statusline(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    result: dict[str, Any] = {}
    if "enabled" in config:
        result["enabled"] = bool(config["enabled"])
    if "refreshMs" in config:
        try:
            r = int(config["refreshMs"])
            if r > 0:
                result["refreshMs"] = r
        except (ValueError, TypeError):
            pass
    if "separator" in config and isinstance(config["separator"], str):
        result["separator"] = config["separator"]

    if "providers" in config and isinstance(config["providers"], list):
        providers: list[dict[str, Any]] = []
        for p in config["providers"]:
            norm = _normalize_statusline_provider(p)
            if norm:
                providers.append(norm)
        result["providers"] = providers
    return result


def _merge_statusline(user: dict | None, project: dict | None) -> dict[str, Any]:
    user_cfg = _normalize_statusline((user or {}).get("statusline"))
    proj_cfg = _normalize_statusline((project or {}).get("statusline"))

    user_providers = user_cfg.get("providers") or []
    proj_providers = proj_cfg.get("providers") or []
    proj_ids = {p.get("id") for p in proj_providers if p.get("id")}
    merged_providers = [
        p for p in user_providers if not (p.get("id") and p["id"] in proj_ids)
    ] + proj_providers

    enabled = proj_cfg.get("enabled", user_cfg.get("enabled", len(merged_providers) > 0))
    refresh_ms = proj_cfg.get("refreshMs", user_cfg.get("refreshMs", DEFAULT_STATUSLINE_REFRESH_MS))
    separator = proj_cfg.get("separator", user_cfg.get("separator", DEFAULT_STATUSLINE_SEPARATOR))

    return {
        "enabled": enabled,
        "refreshMs": refresh_ms,
        "separator": separator,
        "providers": merged_providers,
    }
