"""Shared test setup."""

import os
import tempfile
from pathlib import Path
from coderAI.system.config import config_manager

# Redirect config to a temporary location during tests to prevent local config files from affecting tests
config_manager.config_dir = Path(tempfile.gettempdir()) / ".coderAI_test"
config_manager.config_dir.mkdir(exist_ok=True, parents=True)
config_manager.config_file = config_manager.config_dir / "config.json"

# Provide dummy/mock API keys for testing if they are not present in the environment
# This allows agent and provider instantiation to succeed without requiring real credentials.
for env_key in [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
]:
    if env_key not in os.environ:
        os.environ[env_key] = "mock-key-for-testing"

# Filesystem tools refuse writes outside the project root by default. Tests
# write to pytest's ``tmp_path`` (typically /tmp/...), which is outside this
# repo, so opt out for the test session.
os.environ["CODERAI_ALLOW_OUTSIDE_PROJECT"] = "1"

import pytest

@pytest.fixture(autouse=True)
def _reset_config_each_test():
    """Ensure every test starts with a fresh config cache and empty config file."""
    config_manager._config = None
    if config_manager.config_file.exists():
        try:
            config_manager.config_file.unlink()
        except OSError:
            pass
    # We yield and then reset again on teardown to clean up any modifications made by the test
    yield
    config_manager._config = None
    if config_manager.config_file.exists():
        try:
            config_manager.config_file.unlink()
        except OSError:
            pass
