"""Configuration management for CoderAI."""

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class Config(BaseModel):
    """Configuration model for CoderAI."""

    model_config = ConfigDict(extra="allow")

    openai_api_key: Optional[str] = Field(default=None)
    anthropic_api_key: Optional[str] = Field(default=None)
    groq_api_key: Optional[str] = Field(default=None)
    deepseek_api_key: Optional[str] = Field(default=None)
    default_model: str = Field(default="lmstudio")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=4096)
    lmstudio_endpoint: str = Field(default="http://localhost:1234/v1")
    lmstudio_model: str = Field(default="local-model")
    ollama_endpoint: str = Field(default="http://localhost:11434/v1")
    ollama_model: str = Field(default="llama3")
    web_search_api_key: Optional[str] = Field(default=None)
    web_search_engine: str = Field(default="duckduckgo")  # or 'google', 'bing'
    streaming: bool = Field(default=True)
    reasoning_effort: str = Field(default="medium")  # high, medium, low, none
    budget_limit: float = Field(default=0.0)  # max USD per session, 0 = unlimited
    save_history: bool = Field(default=True)
    context_window: int = Field(default=128000)
    max_iterations: int = Field(default=50)
    max_tool_output: int = Field(default=8000)
    log_level: str = Field(default="WARNING")  # DEBUG, INFO, WARNING, ERROR
    project_instruction_file: str = Field(default="CODERAI.md")
    max_file_size: int = Field(default=1_048_576)  # 1 MB
    max_glob_results: int = Field(default=200)
    max_command_output: int = Field(default=10_000)  # chars
    web_tools_in_main: bool = Field(default=False)  # Allow web tools in main agent
    project_root: str = Field(default=".")


class ConfigManager:
    """Manages configuration for CoderAI."""

    def __init__(self):
        """Initialize the configuration manager."""
        self.config_dir = Path.home() / ".coderAI"
        self.config_file = self.config_dir / "config.json"
        self.config_dir.mkdir(exist_ok=True)
        self._config: Optional[Config] = None

    def load(self) -> Config:
        """Load configuration from file and environment variables."""
        if self._config is not None:
            return self._config

        # Load from file if exists
        config_data = {}
        if self.config_file.exists():
            with open(self.config_file, "r") as f:
                config_data = json.load(f)

        # Override with environment variables
        env_mappings = {
            "OPENAI_API_KEY": "openai_api_key",
            "ANTHROPIC_API_KEY": "anthropic_api_key",
            "GROQ_API_KEY": "groq_api_key",
            "DEEPSEEK_API_KEY": "deepseek_api_key",
            "CODERAI_DEFAULT_MODEL": "default_model",
            "CODERAI_TEMPERATURE": "temperature",
            "CODERAI_MAX_TOKENS": "max_tokens",
            "LMSTUDIO_ENDPOINT": "lmstudio_endpoint",
            "OLLAMA_ENDPOINT": "ollama_endpoint",
            "WEB_SEARCH_API_KEY": "web_search_api_key",
            "CODERAI_LOG_LEVEL": "log_level",
            "CODERAI_BUDGET_LIMIT": "budget_limit",
            "CODERAI_REASONING_EFFORT": "reasoning_effort",
            "CODERAI_MAX_ITERATIONS": "max_iterations",
            "CODERAI_MAX_TOOL_OUTPUT": "max_tool_output",
            "CODERAI_PROJECT_INSTRUCTION_FILE": "project_instruction_file",
        }

        for env_var, config_key in env_mappings.items():
            value = os.getenv(env_var)
            if value is not None:
                # Convert types if needed
                if config_key in ("temperature", "budget_limit"):
                    value = float(value)
                elif config_key in ["max_tokens", "max_iterations", "max_tool_output"]:
                    value = int(value)
                config_data[config_key] = value

        self._config = Config(**config_data)
        return self._config

    def save(self, config: Optional[Config] = None) -> None:
        """Save configuration to file with restricted permissions."""
        if config is None:
            config = self._config
        if config is None:
            return

        with open(self.config_file, "w") as f:
            json.dump(config.model_dump(exclude_none=True), f, indent=2)

        # Set file permissions to owner-only read/write (0600)
        # This protects API keys from being read by other users
        try:
            self.config_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # May fail on some filesystems, not critical

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value."""
        config = self.load()
        setattr(config, key, value)
        self._config = config
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value."""
        config = self.load()
        return getattr(config, key, default)

    def show(self) -> Dict[str, Any]:
        """Get all configuration as a dictionary."""
        config = self.load()
        data = config.model_dump(exclude_none=True)
        # Mask sensitive data
        for key in ["openai_api_key", "anthropic_api_key", "groq_api_key", "deepseek_api_key", "web_search_api_key"]:
            if key in data and data[key]:
                data[key] = f"{data[key][:8]}...{data[key][-4:]}"
        return data

    def reset(self) -> None:
        """Reset configuration to defaults."""
        self._config = Config()
        if self.config_file.exists():
            self.config_file.unlink()

    def load_project_config(self, project_root: str = ".") -> Config:
        """Load per-project config and overlay on a COPY of global config.

        Looks for ``.coderAI/config.json`` in *project_root* and overlays
        the allowed keys on top of a copy of the global configuration.
        The global cached config is NOT mutated.

        Args:
            project_root: Path to the project root directory.

        Returns:
            A new Config instance with project-level overrides applied.
        """
        base = self.load()
        # Work on a deep copy so the global cached config stays pristine
        config = base.model_copy(deep=True)
        config.project_root = str(Path(project_root).resolve())
        project_config_path = Path(project_root).resolve() / ".coderAI" / "config.json"

        if not project_config_path.is_file():
            return config

        # Only these keys may be overridden at the project level.
        # API keys are intentionally excluded for security.
        ALLOWED_PROJECT_KEYS = {
            "default_model",
            "temperature",
            "max_tokens",
            "max_iterations",
            "context_window",
            "max_tool_output",
            "max_file_size",
            "max_glob_results",
            "max_command_output",
            "budget_limit",
            "project_instruction_file",
            "streaming",
            "reasoning_effort",
            "log_level",
        }

        try:
            with open(project_config_path, "r") as f:
                project_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return config

        for key, value in project_data.items():
            if key not in ALLOWED_PROJECT_KEYS:
                continue
            # Type coercion for numeric / boolean fields
            if key == "temperature":
                value = float(value)
            elif key in {
                "max_tokens", "max_iterations", "context_window",
                "max_tool_output", "max_file_size", "max_glob_results",
                "max_command_output",
            }:
                value = int(value)
            elif key == "budget_limit":
                value = float(value)
            elif key == "streaming":
                value = bool(value)
            setattr(config, key, value)

        return config


# Global config manager instance
config_manager = ConfigManager()
