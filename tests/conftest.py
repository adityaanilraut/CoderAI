"""Shared test setup."""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from coderAI.system.config import config_manager

logger = logging.getLogger(__name__)

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

import pytest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402


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


@pytest.fixture
def mock_agent():
    """Shared mock agent fixture for ExecutionLoop-based tests.

    Returns a MagicMock-configured agent with all attributes needed by
    ``ExecutionLoop``, ``pause_turn``, ``length_recovery``, and
    ``loop_backoff`` tests.
    """
    from coderAI.system.history import Session

    agent = MagicMock()
    agent.session = Session(session_id="test")
    agent.config = MagicMock()
    agent.config.max_iterations = 10
    agent.config.max_iterations_hard_cap = 200
    agent.config.budget_limit = 0
    agent.config.continue_loop_on_deny = True
    agent.cost_tracker = MagicMock()
    agent.cost_tracker.get_total_cost.return_value = 0
    agent.provider = MagicMock()
    agent.provider.get_model_info.return_value = {}
    agent.tools = MagicMock()
    agent.tools.get_schemas.return_value = []
    agent.context_controller = MagicMock()
    agent.context_controller.inject_context = lambda msgs, cm, query=None: msgs
    agent.context_controller.manage_context_window = AsyncMock(
        side_effect=lambda msgs: msgs
    )
    agent.context_manager = MagicMock()
    agent._assistant_reply_parts = []
    agent.tracker_info = None
    agent._register_tracker = MagicMock()
    agent._sync_tracker = MagicMock()
    agent._finish_tracker = MagicMock()
    agent.save_session = MagicMock()
    agent.read_cache = None
    agent.hooks_manager = MagicMock()
    agent.hooks_manager.load_hooks.return_value = None
    agent.hooks_manager.run_hooks = AsyncMock(return_value=[])
    return agent


def require_external(command: str, reason: str = "") -> None:
    """Skip a test with a WARNING log when an external binary is not found.

    Unlike plain ``pytest.skip``, this helper always emits a visible
    ``logging.WARNING`` so CI dashboards notice when tests are being
    silently bypassed.
    """
    if not shutil.which(command):
        msg = f"External dependency '{command}' not found in PATH — "
        if reason:
            msg += reason
        else:
            msg += "skipping test"
        logger.warning(msg)
        pytest.skip(f"{command} not installed")
