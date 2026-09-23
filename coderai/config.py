"""Resolve canonical CoderAI settings from files and environment.

Layering (lowest → highest): global ``~/.coderai/mcp.json`` + user settings
(``~/.coderai/settings.json``) -> project settings (``<root>/.coderai/settings.json``)
-> ``--mcp-config-file`` / ``--mcp-config`` CLI overlays -> process env
(CODERAI_*). Project wins over user; CLI overlays and env win over both.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Any, Literal, cast

from coderai.utils.common.model_capabilities import defaults_to_thinking_mode
from coderai.prompt.sections import normalize_tool_preset
from coderai.sandbox import apply_preset, parse_sandbox_mode

from coderai.provider_registry import (
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_WINDOW as DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MODEL as DEFAULT_MODEL,
    KNOWN_PROVIDERS as KNOWN_PROVIDERS,
    PROVIDER_REGISTRY as PROVIDER_REGISTRY,
    get_configured_provider_keys as get_configured_provider_keys,
    save_active_model_setting as save_active_model_setting,
    save_base_url_setting as save_base_url_setting,
    save_custom_endpoint_config as save_custom_endpoint_config,
    save_provider_api_key as save_provider_api_key,
    save_setting_key as save_setting_key,
)
from coderai.typed_config import (
    _chmod_quiet as _chmod_quiet,
    BackgroundConfig as BackgroundConfig,
    Config as Config,
    HookDef as HookDef,
    LLMModel as LLMModel,
    LLMProvider as LLMProvider,
    LoopControl as LoopControl,
    MCPClientConfig as MCPClientConfig,
    MCPConfig as MCPConfig,
    McpConfig as McpConfig,
    MoonshotFetchConfig as MoonshotFetchConfig,
    MoonshotSearchConfig as MoonshotSearchConfig,
    NotificationConfig as NotificationConfig,
    NotificationsConfig as NotificationsConfig,
    OAuthRef as OAuthRef,
    Services as Services,
    TypedConfig as TypedConfig,
    clear_typed_config_cache as clear_typed_config_cache,
    get_config_file as get_config_file,
    get_default_config as get_default_config,
    load_config as load_config,
    load_typed_config as load_typed_config,
    load_typed_config_from_string as load_typed_config_from_string,
    resolve_hierarchical_config_path as resolve_hierarchical_config_path,
    save_config as save_config,
    save_typed_config as save_typed_config,
)

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

ReasoningEffort = Literal["off", "low", "medium", "high", "xhigh", "max"]
DEFAULT_REASONING_EFFORT: ReasoningEffort = "max"
VALID_REASONING_EFFORTS = {"off", "low", "medium", "high", "xhigh", "max"}
_REASONING_EFFORT_ALIASES = {
    "none": "off",
    "disabled": "off",
    "false": "off",
    "0": "off",
}

ToolPreset = Literal["full", "core", "shell_edit"]


def _home() -> pathlib.Path:
    return pathlib.Path.home()


def get_user_settings_path() -> str:
    from coderai.share import get_share_dir

    return str(get_share_dir() / "settings.json")


def get_project_settings_path(project_root: str = ".") -> str:
    return str(pathlib.Path(project_root) / ".coderai" / "settings.json")


# Paths already warned about, so a broken config prints one stderr warning
# instead of spamming every settings resolution (IN-A14).
_warned_parse_errors: set[str] = set()


def _warn_parse_error_once(path: str, detail: str) -> None:
    if path in _warned_parse_errors:
        return
    _warned_parse_errors.add(path)
    try:
        print(f"coderai: warning: ignoring invalid config {path}: {detail}", file=sys.stderr)
    except OSError:
        pass


def _read_settings_file(path: str) -> dict | None:
    try:
        p = pathlib.Path(path)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        _warn_parse_error_once(path, "top-level JSON value is not an object")
        return None
    except (OSError, ValueError) as exc:
        _warn_parse_error_once(path, str(exc) or exc.__class__.__name__)
        return None


def read_settings() -> dict | None:
    return _read_settings_file(get_user_settings_path())


def read_project_settings(project_root: str = ".") -> dict | None:
    return _read_settings_file(get_project_settings_path(project_root))


def tighten_config_permissions() -> None:
    """Clamp user-scope config dirs/files to 0700/0600 (IN-A2).

    Idempotent; safe to call at startup. Never touches the repository.
    """
    user_dir = _home() / ".coderai"
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _chmod_quiet(user_dir, 0o700)
    try:
        from coderai.share import get_share_dir

        _chmod_quiet(get_share_dir(), 0o700)
    except OSError:
        pass
    for name in ("settings.json", ".env", "config.toml"):
        p = user_dir / name
        try:
            if p.is_file():
                _chmod_quiet(p, 0o600)
        except OSError:
            pass
    try:
        from coderai.share import get_share_dir as _share_dir

        store = _share_dir() / "trusted_projects.json"
        if store.is_file():
            _chmod_quiet(store, 0o600)
    except OSError:
        pass


def _write_settings_file(path: str, settings: dict) -> None:
    from coderai.utils.io import atomic_json_write

    p = pathlib.Path(path)
    try:
        user_dir = _home() / ".coderai"
        try:
            same = p.parent.resolve() == user_dir.resolve()
        except OSError:
            same = False
        if same:
            p.parent.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(p.parent, 0o700)
    except OSError:
        pass
    # atomic_json_write uses mkstemp (0600); keep it that way for secrets.
    atomic_json_write(settings, p)
    _chmod_quiet(p, 0o600)


def write_settings(settings: dict) -> None:
    _write_settings_file(get_user_settings_path(), settings)


def write_project_settings(settings: dict, project_root: str = ".") -> None:
    _write_settings_file(get_project_settings_path(project_root), settings)


def load_dotenv(project_root: str = ".", *, trusted: bool | None = None) -> dict[str, str]:
    """Parse key=value pairs from .env in project root and ~/.coderai/.env into os.environ.

    The project ``.env`` is skipped until the project is trusted (IN-A1);
    the user-scope ``~/.coderai/.env`` always loads.
    """
    if trusted is None:
        from coderai.trust import is_project_trusted

        trusted = is_project_trusted(project_root)
    loaded: dict[str, str] = {}
    from coderai.share import get_share_dir

    candidates = [
        (get_share_dir() / ".env", True),
        (pathlib.Path(project_root) / ".env", bool(trusted)),
    ]
    for p, allowed in candidates:
        if not allowed or not p.is_file():
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
        except (OSError, ValueError):
            pass
    return loaded


def resolve_typed_config_overlay(project_root: str = ".") -> dict[str, Any]:
    """Best-effort typed-config (``config.toml``) overlay for legacy resolution.

     Returns ``{}`` when no typed config exists or it fails validation — the
     legacy ``settings.json`` + env path remains authoritative. Explicit
     ``CODERAI_CONFIG_FILE`` / ``CODERAI_CONFIG_STRING`` redirects are honored
    .
    """
    # NOTE: load_typed_config / load_typed_config_from_string live in this
    # same module (defined below); reference the module globals directly so
    # tests can monkeypatch coderai.config.load_typed_config as before.
    try:
        override_file = os.environ.get("CODERAI_CONFIG_FILE")
        override_text = os.environ.get("CODERAI_CONFIG_STRING")
        if override_text:
            typed = load_typed_config_from_string(override_text)
        elif override_file:
            typed = load_typed_config(
                pathlib.Path(override_file).expanduser(), project_root=project_root
            )
        else:
            typed = load_typed_config(project_root=project_root)
    except Exception as exc:
        # Best-effort overlay: any load/validation failure (including
        # unexpected errors from patched loaders in tests) falls back to
        # legacy settings.json + env resolution.
        _warn_parse_error_once("typed-config", str(exc) or exc.__class__.__name__)
        return {}
    try:
        if not typed.default_model or typed.default_model not in typed.models:
            return _typed_global_knobs(typed)
        model = typed.models[typed.default_model]
        provider = typed.providers.get(model.provider)
        if provider is None:
            return _typed_global_knobs(typed)
        return {
            **_typed_global_knobs(typed),
            "model": model.model,
            "baseURL": provider.base_url,
            "apiKey": provider.api_key.get_secret_value(),
            "maxContextSize": model.max_context_size,
            "capabilities": sorted(model.capabilities or ()),
            "displayName": model.display_name,
            "providerType": provider.type,
            "loopControl": {
                "maxStepsPerTurn": typed.loop_control.max_steps_per_turn,
                "maxRetriesPerStep": typed.loop_control.max_retries_per_step,
                "maxRalphIterations": typed.loop_control.max_ralph_iterations,
                "reservedContextSize": typed.loop_control.reserved_context_size,
                "compactionTriggerRatio": typed.loop_control.compaction_trigger_ratio,
            },
            "isDefaultLocation": typed.is_from_default_location,
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}


def _typed_global_knobs(typed: Any) -> dict[str, Any]:
    """Model-independent knobs from the typed config."""
    try:
        knobs: dict[str, Any] = {
            "mergeAllAvailableSkills": bool(typed.merge_all_available_skills),
            "extraSkillDirs": list(typed.extra_skill_dirs or []),
            "notificationsClaimStaleAfterMs": int(typed.notifications.claim_stale_after_ms),
            "mcpToolCallTimeoutMs": int(typed.mcp.client.tool_call_timeout_ms),
        }
        if getattr(typed, "telemetry", None) is not None:
            knobs["telemetry"] = bool(typed.telemetry)
            knobs["telemetryEnabled"] = bool(typed.telemetry)
        return knobs
    except (AttributeError, TypeError, ValueError):
        return {}


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
        if n in ("1", "true", "enable", "enabled", "yes", "on"):
            return True
        if n in ("0", "false", "disable", "disabled", "no", "off"):
            return False
    return None


def parse_reasoning_effort(value: Any) -> ReasoningEffort | None:
    """Normalize a reasoning-effort setting to off|low|medium|high|xhigh|max."""
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    normalized = _REASONING_EFFORT_ALIASES.get(raw, raw)
    if normalized in VALID_REASONING_EFFORTS:
        return cast(ReasoningEffort, normalized)
    return None


def parse_tool_preset(value: Any) -> ToolPreset | None:
    """Accept only canonical tool preset names."""
    normalized = normalize_tool_preset(value)
    return cast(ToolPreset, normalized) if normalized is not None else None


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
    return value if value in ("allowAll", "askAll") else "askAll"


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
    # GPT-6 family: 1.05M context window (922k max input, 128k max output).
    # GPT-5.6 family shares the same 1.05M window.
    if "gpt-6" in m or "gpt-5.6" in m or "astra" in m:
        return 1_050_000
    if any(k in m for k in ("gpt-5", "gpt-4.5", "claude-3-7", "gemini-2.5", "gemini-2.0")):
        return 512 * 1024
    return DEFAULT_CONTEXT_WINDOW


def get_default_auto_compact_window(model: str = "") -> int:
    return max(1, get_default_context_window(model) // 2)


# Project-scope keys that can execute commands or steer secrets. Ignored
# until the project is trusted (IN-A1).
_UNTRUSTED_PROJECT_DENY_KEYS = frozenset(
    {"mcpServers", "baseURL", "env", "statusline", "hooks", "webSearchTool"}
)


def _strip_untrusted_project_settings(project: dict) -> dict:
    """Drop command/secret-steering keys from an untrusted project's settings."""
    return {k: v for k, v in project.items() if k not in _UNTRUSTED_PROJECT_DENY_KEYS}


def settings_env_value(name: str, project_root: str = ".") -> str | None:
    """Value of ``name`` from the settings ``env`` blocks; project wins only when trusted."""
    from coderai.trust import is_project_trusted

    envs = [_normalize_env((read_settings() or {}).get("env"))]
    if is_project_trusted(project_root):
        envs.insert(0, _normalize_env((read_project_settings(project_root) or {}).get("env")))
    for env in envs:
        val = env.get(name, "").strip()
        if val:
            return val
    return None


def resolve_current_settings(
    project_root: str = ".", *, trusted: bool | None = None
) -> dict[str, Any]:
    """Resolve user + project + env into a single settings dict."""
    if trusted is None:
        from coderai.trust import is_project_trusted

        trusted = is_project_trusted(project_root)
    load_dotenv(project_root, trusted=trusted)
    user = read_settings() or {}
    project = read_project_settings(project_root) or {}
    if not trusted:
        project = _strip_untrusted_project_settings(project)

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

    def first_parsed(parser: Any, *values: Any) -> Any:
        for value in values:
            parsed = parser(value)
            if parsed is not None:
                return parsed
        return None

    typed_overlay = resolve_typed_config_overlay(project_root)
    if not trusted and not (
        os.environ.get("CODERAI_CONFIG_FILE") or os.environ.get("CODERAI_CONFIG_STRING")
    ):
        # Never pair a user-scope apiKey with a project config.toml baseURL
        # until the project is trusted (IN-A1). An explicit config-file
        # override is the user's own intent, so it stays.
        typed_overlay.pop("baseURL", None)
        typed_overlay.pop("apiKey", None)

    # merge_all_available_skills (default True).
    _merge_all_parsed = first_parsed(
        _parse_bool,
        system_env.get("MERGE_ALL_AVAILABLE_SKILLS"),
        project.get("mergeAllAvailableSkills"),
        user.get("mergeAllAvailableSkills"),
        typed_overlay.get("mergeAllAvailableSkills"),
    )
    merge_all_available_skills = True if _merge_all_parsed is None else _merge_all_parsed

    is_project_typed = not typed_overlay.get("isDefaultLocation", True)

    env_model = first(system_env.get("MODEL"))
    env_base_url = first(system_env.get("BASE_URL"))
    env_api_key = first(system_env.get("API_KEY"))

    if _trim(project.get("model")):
        model = _trim(project.get("model"))
        base_url = (
            _trim(project.get("baseURL")) or _trim(typed_overlay.get("baseURL")) or DEFAULT_BASE_URL
        )
        api_key = _trim(project.get("apiKey")) or _trim(typed_overlay.get("apiKey")) or None
    elif is_project_typed and _trim(typed_overlay.get("model")):
        model = _trim(typed_overlay.get("model"))
        base_url = _trim(typed_overlay.get("baseURL")) or DEFAULT_BASE_URL
        api_key = _trim(typed_overlay.get("apiKey")) or None
    elif _trim(user.get("model")):
        model = _trim(user.get("model"))
        base_url = (
            _trim(user.get("baseURL"))
            or (_trim(typed_overlay.get("baseURL")) if not is_project_typed else "")
            or DEFAULT_BASE_URL
        )
        api_key = (
            _trim(user.get("apiKey"))
            or (_trim(typed_overlay.get("apiKey")) if not is_project_typed else "")
            or None
        )
    elif not is_project_typed and _trim(typed_overlay.get("model")):
        model = _trim(typed_overlay.get("model"))
        base_url = _trim(typed_overlay.get("baseURL")) or DEFAULT_BASE_URL
        api_key = _trim(typed_overlay.get("apiKey")) or None
    else:
        model = DEFAULT_MODEL
        base_url = (
            _trim(project.get("baseURL"))
            or (is_project_typed and _trim(typed_overlay.get("baseURL")))
            or _trim(user.get("baseURL"))
            or _trim(typed_overlay.get("baseURL"))
            or DEFAULT_BASE_URL
        )
        api_key = (
            _trim(project.get("apiKey"))
            or (is_project_typed and _trim(typed_overlay.get("apiKey")))
            or _trim(user.get("apiKey"))
            or _trim(typed_overlay.get("apiKey"))
            or None
        )

    if env_model:
        model = env_model
    if env_base_url:
        base_url = env_base_url
    if env_api_key:
        api_key = env_api_key

    # OPENAI_* fallback for compat.
    if not api_key and os.getenv("OPENAI_API_KEY"):
        api_key = os.getenv("OPENAI_API_KEY")
    if base_url == DEFAULT_BASE_URL and os.getenv("OPENAI_BASE_URL"):
        base_url = os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL

    configured_context_window = first_token_window(
        system_env.get("CONTEXT_WINDOW"),
        project.get("contextWindow"),
        user.get("contextWindow"),
        typed_overlay.get("maxContextSize"),
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

    # loop_control knobs
    def _parse_int(v: Any) -> int | None:
        try:
            iv = int(str(v).strip())
            return iv if iv > 0 else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _parse_float(v: Any) -> float | None:
        try:
            fv = float(str(v).strip())
            return fv if 0 < fv < 1 else None
        except (AttributeError, TypeError, ValueError):
            return None

    max_steps_per_turn = (
        first_parsed(
            _parse_int,
            system_env.get("MAX_STEPS_PER_TURN"),
            project.get("maxStepsPerTurn"),
            user.get("maxStepsPerTurn"),
        )
        or 1000
    )
    reserved_context_size = (
        first_parsed(
            _parse_int,
            system_env.get("RESERVED_CONTEXT_SIZE"),
            project.get("reservedContextSize"),
            user.get("reservedContextSize"),
        )
        or 50_000
    )
    compaction_trigger_ratio = (
        first_parsed(
            _parse_float,
            system_env.get("COMPACTION_TRIGGER_RATIO"),
            project.get("compactionTriggerRatio"),
            user.get("compactionTriggerRatio"),
        )
        or 0.85
    )

    configured_thinking = first_parsed(
        _parse_bool,
        system_env.get("THINKING_ENABLED"),
        project.get("thinkingEnabled"),
        user.get("thinkingEnabled"),
    )
    thinking_enabled = (
        configured_thinking if configured_thinking is not None else defaults_to_thinking_mode(model)
    )

    temperature = first_parsed(
        _parse_temperature,
        system_env.get("TEMPERATURE"),
        project.get("temperature"),
        user.get("temperature"),
    )

    multimodal = (
        _resolve_multimodal_mode(system_env.get("MULTIMODAL"))
        or _resolve_multimodal_mode(project.get("multimodal"))
        or _resolve_multimodal_mode(user.get("multimodal"))
        or "default"
    )

    resolved: dict[str, Any] = {
        "env": env,
        "apiKey": api_key,
        "baseURL": base_url,
        "model": model,
        "contextWindow": context_window,
        "autoCompactWindow": auto_compact_window,
        "temperature": temperature,
        "thinkingEnabled": thinking_enabled,
        "reasoningEffort": (
            parse_reasoning_effort(system_env.get("REASONING_EFFORT"))
            or parse_reasoning_effort(project.get("reasoningEffort"))
            or parse_reasoning_effort(user.get("reasoningEffort"))
            or DEFAULT_REASONING_EFFORT
        ),
        "debugLogEnabled": bool(
            first_parsed(
                _parse_bool,
                system_env.get("DEBUG_LOG_ENABLED"),
                project.get("debugLogEnabled"),
                user.get("debugLogEnabled"),
            )
        ),
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
        "toolsPreset": first_parsed(
            parse_tool_preset,
            system_env.get("TOOLS_PRESET"),
            project.get("toolsPreset"),
            user.get("toolsPreset"),
        ),
        "mcpServers": _merge_mcp_servers(user, project, user_env, project_env, system_env),
        "permissions": apply_preset(
            _merge_permissions(user, project),
            parse_sandbox_mode(
                first(
                    system_env.get("PERMISSION_PRESET"),
                    ((project.get("permissions") or {}).get("preset")),
                    ((user.get("permissions") or {}).get("preset")),
                )
            ),
        ),
        "enabledSkills": _merge_enabled_skills(user, project),
        "skillScanPaths": _merge_skill_scan_paths(
            user, project, typed_overlay.get("extraSkillDirs")
        ),
        "mergeAllAvailableSkills": merge_all_available_skills,
        "notificationsClaimStaleAfterMs": (
            first_parsed(
                _parse_int,
                system_env.get("NOTIFICATIONS_CLAIM_STALE_AFTER_MS"),
                (project.get("notifications") or {}).get("claimStaleAfterMs")
                if isinstance(project.get("notifications"), dict)
                else project.get("notificationsClaimStaleAfterMs"),
                (user.get("notifications") or {}).get("claimStaleAfterMs")
                if isinstance(user.get("notifications"), dict)
                else user.get("notificationsClaimStaleAfterMs"),
                typed_overlay.get("notificationsClaimStaleAfterMs"),
            )
            or 15_000
        ),
        "mcpToolCallTimeoutMs": (
            first_parsed(
                _parse_int,
                system_env.get("MCP_TOOL_CALL_TIMEOUT_MS"),
                project.get("mcpToolCallTimeoutMs"),
                user.get("mcpToolCallTimeoutMs"),
                typed_overlay.get("mcpToolCallTimeoutMs"),
            )
            or 60_000
        ),
        "fallbackModels": _resolve_fallback_models(user, project, system_env),
        "statusline": _merge_statusline(user, project),
        "maxStepsPerTurn": max_steps_per_turn,
        "reservedContextSize": reserved_context_size,
        "compactionTriggerRatio": compaction_trigger_ratio,
        # Typed-config overlay passthrough (provider vocabulary + loop control).
        "providerType": typed_overlay.get("providerType") or "openai_legacy",
        "capabilities": typed_overlay.get("capabilities") or [],
        "displayName": typed_overlay.get("displayName"),
        "typedMaxContextSize": typed_overlay.get("maxContextSize"),
        "typedLoopControl": typed_overlay.get("loopControl") or {},
        "typedConfigDefaultLocation": typed_overlay.get("isDefaultLocation", False),
    }
    _telemetry_val = first_parsed(
        _parse_bool,
        system_env.get("TELEMETRY"),
        project.get("telemetry"),
        project.get("telemetryEnabled"),
        user.get("telemetry"),
        user.get("telemetryEnabled"),
        typed_overlay.get("telemetry"),
        typed_overlay.get("telemetryEnabled"),
    )
    if _telemetry_val is not None:
        resolved["telemetry"] = _telemetry_val
        resolved["telemetryEnabled"] = _telemetry_val
    # Back-compat alias: snake_case mirrors camelCase (single computed list).
    resolved["fallback_models"] = resolved["fallbackModels"]
    return resolved


def _normalize_model_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(p).strip() for p in value if isinstance(p, str) and p.strip()]
    return []


def _resolve_fallback_models(
    user: dict | None, project: dict | None, system_env: dict | None
) -> list[str]:
    env_models = _normalize_model_list((system_env or {}).get("FALLBACK_MODELS"))
    if env_models:
        return env_models
    proj_models = _normalize_model_list(
        (project or {}).get("fallbackModels") or (project or {}).get("fallback_models")
    )
    if proj_models:
        return proj_models
    user_models = _normalize_model_list(
        (user or {}).get("fallbackModels") or (user or {}).get("fallback_models")
    )
    if user_models:
        return user_models
    return []


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


def _merge_skill_scan_paths(
    user: dict | None, project: dict | None, extra: Any = None
) -> list[str]:
    u_paths = _normalize_skill_scan_paths((user or {}).get("skillScanPaths"))
    p_paths = _normalize_skill_scan_paths((project or {}).get("skillScanPaths"))
    x_paths = _normalize_skill_scan_paths(extra)
    combined: list[str] = []
    for p in u_paths + p_paths + x_paths:
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
    """Merge MCP servers: global file → user → project → CLI overlays.

    Global ``~/.coderai/mcp.json`` seeds the merge;
    ``--mcp-config-file`` / ``--mcp-config`` overlays win per server.
    """
    from coderai.mcp.files import (
        collect_cli_mcp_overlays,
        load_global_mcp_servers,
        merge_mcp_servers_dicts,
    )

    user_servers = user.get("mcpServers") or {}
    project_servers = project.get("mcpServers") or {}
    if not isinstance(user_servers, dict):
        user_servers = {}
    if not isinstance(project_servers, dict):
        project_servers = {}
    names = set(user_servers) | set(project_servers)
    merged: dict[str, dict] = {}
    for name in names:
        if not isinstance(name, str) or not name:
            continue
        cfg = _merge_mcp_server_config(user_servers.get(name), project_servers.get(name))
        if cfg:
            merged[name] = cfg
    layered = merge_mcp_servers_dicts(load_global_mcp_servers(), merged or None)
    cli_overlays, _warnings = collect_cli_mcp_overlays()
    layered = merge_mcp_servers_dicts(layered, cli_overlays or None)
    return layered or None


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


def mask_api_key(key: str | None) -> str:
    """Mask secret API key showing only first 4 and last 3 characters."""
    if not key or not isinstance(key, str) or not key.strip():
        return "Not Set"
    s = key.strip()
    if len(s) <= 8:
        return "****"
    return f"{s[:4]}...{s[-3:]}"
