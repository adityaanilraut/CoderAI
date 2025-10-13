"""Configuration management for CoderAI."""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Config(BaseModel):
    """Configuration model for CoderAI."""

    openai_api_key: Optional[str] = Field(default=None)
    default_model: str = Field(default="lmstudio")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=4096)
    lmstudio_endpoint: str = Field(default="http://localhost:1234/v1")
    lmstudio_model: str = Field(default="local-model")
    web_search_api_key: Optional[str] = Field(default=None)
    web_search_engine: str = Field(default="duckduckgo")  # or 'google', 'bing'
    streaming: bool = Field(default=True)
    save_history: bool = Field(default=True)
    context_window: int = Field(default=128000)

    class Config:
        """Pydantic config."""

        extra = "allow"


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
            "CODERAI_DEFAULT_MODEL": "default_model",
            "CODERAI_TEMPERATURE": "temperature",
            "CODERAI_MAX_TOKENS": "max_tokens",
            "LMSTUDIO_ENDPOINT": "lmstudio_endpoint",
            "WEB_SEARCH_API_KEY": "web_search_api_key",
        }

        for env_var, config_key in env_mappings.items():
            value = os.getenv(env_var)
            if value is not None:
                # Convert types if needed
                if config_key == "temperature":
                    value = float(value)
                elif config_key == "max_tokens":
                    value = int(value)
                config_data[config_key] = value

        self._config = Config(**config_data)
        return self._config

    def save(self, config: Optional[Config] = None) -> None:
        """Save configuration to file."""
        if config is None:
            config = self._config
        if config is None:
            return

        with open(self.config_file, "w") as f:
            json.dump(config.model_dump(exclude_none=True), f, indent=2)

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
        if "openai_api_key" in data and data["openai_api_key"]:
            data["openai_api_key"] = f"{data['openai_api_key'][:8]}...{data['openai_api_key'][-4:]}"
        if "web_search_api_key" in data and data["web_search_api_key"]:
            data["web_search_api_key"] = f"{data['web_search_api_key'][:8]}...{data['web_search_api_key'][-4:]}"
        return data

    def reset(self) -> None:
        """Reset configuration to defaults."""
        self._config = Config()
        if self.config_file.exists():
            self.config_file.unlink()


# Global config manager instance
config_manager = ConfigManager()

