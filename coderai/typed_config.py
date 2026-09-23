"""Validated TOML/JSON configuration (typed config layer).

This is the *typed* config layer: ``providers`` / ``models`` / ``services`` /
``loop_control`` / ``hooks`` validated with pydantic, loaded from
``~/.coderai/config.toml`` (JSON also accepted). It coexists with the legacy
dict-based ``settings.py`` (``settings.json`` + ``CODERAI_*`` env) — migration
copies legacy keys forward; ``TypedConfig`` is authoritative for the new
provider/model vocabulary (``llm_types``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Self

from coderai.exception import ConfigError
from coderai.llm import ModelCapability, ProviderType
from coderai.log import logger
from coderai.share import get_share_dir

try:
    import tomlkit
    from tomlkit.exceptions import TOMLKitError
except ImportError:  # minimal installs: JSON-only mode
    tomlkit = None  # type: ignore[assignment]

    class TOMLKitError(Exception):  # type: ignore[no-redef]
        pass


from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    model_validator,
)


def _chmod_quiet(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


class OAuthRef(BaseModel):
    """Reference to OAuth credentials stored outside the config file."""

    storage: Literal["keyring", "file"] = "file"
    key: str


class LLMProvider(BaseModel):
    """LLM provider configuration."""

    type: ProviderType
    base_url: str
    api_key: SecretStr
    env: dict[str, str] | None = None
    custom_headers: dict[str, str] | None = None
    reasoning_key: str | None = None
    oauth: OAuthRef | None = None

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr) -> str:
        return v.get_secret_value()


class LLMModel(BaseModel):
    """LLM model configuration."""

    provider: str
    model: str
    max_context_size: int
    capabilities: set[ModelCapability] | None = None
    display_name: str | None = None


class LoopControl(BaseModel):
    """Agent loop control configuration."""

    max_steps_per_turn: int = Field(
        default=1000,
        ge=1,
        validation_alias=AliasChoices("max_steps_per_turn", "max_steps_per_run"),
    )
    max_retries_per_step: int = Field(default=3, ge=1)
    max_ralph_iterations: int = Field(default=0, ge=-1)
    reserved_context_size: int = Field(default=50_000, ge=1000)
    compaction_trigger_ratio: float = Field(default=0.85, ge=0.5, le=0.99)


class MoonshotSearchConfig(BaseModel):
    """Moonshot Search service configuration."""

    base_url: str
    api_key: SecretStr
    custom_headers: dict[str, str] | None = None
    oauth: OAuthRef | None = None

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr) -> str:
        return v.get_secret_value()


class MoonshotFetchConfig(BaseModel):
    """Moonshot Fetch service configuration."""

    base_url: str
    api_key: SecretStr
    custom_headers: dict[str, str] | None = None
    oauth: OAuthRef | None = None

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr) -> str:
        return v.get_secret_value()


class Services(BaseModel):
    """External tool-backing services."""

    moonshot_search: MoonshotSearchConfig | None = None
    moonshot_fetch: MoonshotFetchConfig | None = None


class HookDef(BaseModel):
    """A single hook definition in config.toml."""

    event: str
    command: str
    matcher: str = ""
    timeout: int = Field(default=30, ge=1, le=600)


class BackgroundConfig(BaseModel):
    """Background task runtime configuration."""

    max_running_tasks: int = Field(default=4, ge=1)
    read_max_bytes: int = Field(default=30_000, ge=1024)
    notification_tail_lines: int = Field(default=20, ge=1)
    notification_tail_chars: int = Field(default=3_000, ge=256)
    wait_poll_interval_ms: int = Field(default=500, ge=50)
    worker_heartbeat_interval_ms: int = Field(default=5_000, ge=100)
    worker_stale_after_ms: int = Field(default=15_000, ge=1000)
    kill_grace_period_ms: int = Field(default=2_000, ge=100)
    keep_alive_on_exit: bool = Field(
        default=False,
        description="Keep background tasks alive when CLI exits. Default: kill on exit.",
    )
    agent_task_timeout_s: int = Field(default=900, ge=60)
    print_wait_ceiling_s: int = Field(default=3600, ge=1)


class NotificationsConfig(BaseModel):
    """Notification delivery tuning."""

    claim_stale_after_ms: int = Field(default=15_000, ge=1000)


NotificationConfig = NotificationsConfig


class MCPClientConfig(BaseModel):
    """MCP client tuning."""

    tool_call_timeout_ms: int = Field(default=60_000, ge=1000)


class MCPConfig(BaseModel):
    """MCP configuration."""

    client: MCPClientConfig = Field(default_factory=MCPClientConfig)


McpConfig = MCPConfig


class TypedConfig(BaseModel):
    """Main validated configuration structure."""

    is_from_default_location: bool = Field(default=False, exclude=True)
    source_file: Path | None = Field(default=None, exclude=True)
    default_model: str = Field(default="")
    default_thinking: bool = Field(default=False)
    default_yolo: bool = Field(default=False)
    skip_afk_prompt_injection: bool = Field(default=False)
    default_plan_mode: bool = Field(default=False)
    default_editor: str = Field(default="")
    theme: Literal["dark", "light"] = Field(default="dark")
    show_thinking_stream: bool = Field(default=True)
    models: dict[str, LLMModel] = Field(default_factory=dict)
    providers: dict[str, LLMProvider] = Field(default_factory=dict)
    loop_control: LoopControl = Field(default_factory=LoopControl)
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)
    services: Services = Field(default_factory=Services)
    hooks: list[HookDef] = Field(default_factory=list)
    telemetry: bool = Field(default=False)
    merge_all_available_skills: bool = Field(default=True)
    extra_skill_dirs: list[str] = Field(default_factory=list)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        if self.default_model and self.default_model not in self.models:
            for key, model in self.models.items():
                if model.model == self.default_model:
                    self.default_model = key
                    break
        for name, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(
                    f"Model {name!r}: provider {model.provider!r} not found in providers"
                )
        return self


# Alias
Config = TypedConfig


def get_config_file() -> Path:
    """Default typed-config path (``~/.coderai/config.toml``)."""
    return get_share_dir() / "config.toml"


def get_default_config() -> TypedConfig:
    """Empty validated config (no models/providers)."""
    return TypedConfig(default_model="", models={}, providers={}, services=Services())


def _parse_text(config_text: str, source: str) -> dict:
    text = config_text.strip()
    if not text:
        raise ConfigError("Configuration text cannot be empty")
    json_error: Exception | None = None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as exc:
        json_error = exc
    if tomlkit is None:
        raise ConfigError(f"Invalid JSON in {source}: {json_error} (TOML unavailable)")
    try:
        data = tomlkit.loads(text)
    except TOMLKitError as exc:
        raise ConfigError(f"Invalid configuration in {source}: {json_error}; {exc}") from exc
    return dict(data)


#: In-process cache for ``load_typed_config`` keyed by
#: ``(resolved path, mtime_ns, size)``. Typed-config parsing + pydantic
#: validation runs on every ``resolve_current_settings`` /
#: ``create_openai_client`` call (i.e. on the TTFT path of every turn), so
#: caching it removes repeated TOML parse + validation when the file is
#: unchanged. Invalidated by mtime/size and by ``save_typed_config``.
_typed_config_cache: dict[tuple[str, int, int, int, int], TypedConfig] = {}


def clear_typed_config_cache() -> None:
    """Drop cached typed configs (tests / explicit config rewrites)."""
    _typed_config_cache.clear()


def resolve_hierarchical_config_path(
    config_file: Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> tuple[Path, bool]:
    """Resolve configuration file path following the strict cascade:
    1. Explicit path (CLI Flag)
    2. CODERAI_CONFIG_FILE environment variable
    3. Workspace local: <project_root>/.coderai/config.toml
    4. XDG global: ~/.config/coderai/config.toml
    5. Share global: ~/.coderai/config.toml (default)

    Returns (resolved_path, is_default_location).
    """
    default_path = get_config_file().expanduser().resolve(strict=False)
    if config_file is not None:
        p = config_file.expanduser().resolve(strict=False)
        return p, p == default_path

    env_override = os.environ.get("CODERAI_CONFIG_FILE")
    if env_override:
        p = Path(env_override).expanduser().resolve(strict=False)
        return p, p == default_path

    # Check workspace local .coderai/config.toml
    root_path = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    local_cfg = root_path / ".coderai" / "config.toml"
    if local_cfg.is_file():
        return local_cfg.resolve(strict=False), False

    # Check XDG global config
    xdg_cfg = Path.home() / ".config" / "coderai" / "config.toml"
    if xdg_cfg.is_file():
        return xdg_cfg.resolve(strict=False), False

    return default_path, True


def load_typed_config(
    config_file: Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> TypedConfig:
    """Load + validate config following the hierarchical configuration cascade."""
    default_path = get_config_file().expanduser().resolve(strict=False)
    config_file, is_default = resolve_hierarchical_config_path(
        config_file, project_root=project_root
    )
    logger.debug("Loading typed config from file: {file}", file=str(config_file))

    if is_default and not config_file.exists():
        _migrate_legacy_settings_once()

    if not config_file.exists():
        config = get_default_config()
        config.is_from_default_location = is_default
        config.source_file = config_file
        return config

    try:
        stat = config_file.stat()
        def_stat = (
            default_path.stat()
            if (not is_default and config_file != default_path and default_path.is_file())
            else None
        )
        cache_key = (
            str(config_file),
            stat.st_mtime_ns,
            stat.st_size,
            def_stat.st_mtime_ns if def_stat else 0,
            def_stat.st_size if def_stat else 0,
        )
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _typed_config_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        data = _parse_text(config_file.read_text(encoding="utf-8"), str(config_file))
        # If loading workspace local config, merge over global definitions when available
        if not is_default and config_file != default_path and default_path.is_file():
            try:
                global_data = _parse_text(
                    default_path.read_text(encoding="utf-8"), str(default_path)
                )
                merged_providers = dict(global_data.get("providers") or {})
                merged_providers.update(data.get("providers") or {})
                merged_models = dict(global_data.get("models") or {})
                merged_models.update(data.get("models") or {})
                data["providers"] = merged_providers
                data["models"] = merged_models
                if "default_model" not in data and "default_model" in global_data:
                    data["default_model"] = global_data["default_model"]
            except Exception as exc:
                logger.debug("Global config cascade merge skipped: {error}", error=exc)
        config = TypedConfig.model_validate(data)
    except ConfigError:
        raise
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration file {config_file}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Cannot read configuration file {config_file}: {e}") from e
    config.is_from_default_location = is_default
    config.source_file = config_file
    if cache_key is not None:
        # Bound growth: entries are keyed by (path, mtime, size); stale keys
        # are naturally orphaned on rewrite. Keep the newest few only.
        if len(_typed_config_cache) > 16:
            _typed_config_cache.clear()
        _typed_config_cache[cache_key] = config
    return config


def load_typed_config_from_string(config_string: str) -> TypedConfig:
    """Load + validate config from a TOML/JSON string (never default-located)."""
    try:
        config = TypedConfig.model_validate(_parse_text(config_string, "configuration text"))
    except ConfigError:
        raise
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration text: {e}") from e
    config.is_from_default_location = False
    config.source_file = None
    return config


def save_typed_config(config: TypedConfig, config_file: Path | None = None) -> None:
    """Persist config as TOML (or JSON for ``.json`` paths), atomically as 0600."""
    from coderai.utils.io import atomic_write_text

    target = config_file or get_config_file()
    logger.debug("Saving typed config to file: {file}", file=str(target))
    # Invalidate cached loads of this path (content is about to change).
    try:
        resolved = str(target.expanduser().resolve(strict=False))
        for key in [k for k in _typed_config_cache if k[0] == resolved]:
            _typed_config_cache.pop(key, None)
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    if target.suffix.lower() == ".json" or tomlkit is None:
        # atomic_write_text uses mkstemp (0600); secrets stay owner-only.
        atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2))
    else:
        atomic_write_text(target, tomlkit.dumps(data))
    _chmod_quiet(target, 0o600)


# Aliases
load_config = load_typed_config
save_config = save_typed_config


def _migrate_legacy_settings_once() -> None:
    """One-time migration from legacy ``settings.json`` to ``config.toml``.

    Copies ``model``/``baseURL``/``apiKey`` into a single ``default`` model +
    ``default`` provider. Never overwrites an existing ``config.toml``.
    """
    target = get_config_file()
    if target.exists():
        return
    legacy = get_share_dir() / "settings.json"
    if not legacy.is_file():
        return
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    try:
        model_name = str(raw.get("model") or "").strip()
        base_url = str(raw.get("baseURL") or "").strip()
        api_key = str(raw.get("apiKey") or "").strip()
        if not (model_name and base_url and api_key):
            return
        config = get_default_config()
        config.providers["default"] = LLMProvider(
            type="openai_legacy",
            base_url=base_url,
            api_key=SecretStr(api_key),
        )
        config.models["default"] = LLMModel(
            provider="default",
            model=model_name,
            max_context_size=256 * 1024,
        )
        config.default_model = "default"
        save_typed_config(config, target)
        logger.info("Migrated legacy settings.json to {file}", file=str(target))
    except (ValidationError, OSError) as exc:
        logger.warning("Legacy settings migration skipped: {error}", error=exc)
