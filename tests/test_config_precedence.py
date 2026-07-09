"""Regression tests: save() must not freeze code defaults into config.json.

Historically ``ConfigManager.save()`` dumped the full model, so every default
got written to ``~/.coderAI/config.json`` on the first ``config set``. After
that, changing a default in code (e.g. ``web_tools_in_main``) never took
effect for existing users. These tests pin the fixed behavior.
"""

import json
import os
from pathlib import Path

import pytest

from coderAI.system.config import Config, ConfigManager


@pytest.fixture
def manager(tmp_path):
    m = ConfigManager()
    m.config_dir = tmp_path
    m.config_file = tmp_path / "config.json"
    m._config = None
    m._explicit_keys = set()
    m._env_keys = set()
    return m


class TestSaveMinimal:
    def test_set_persists_only_that_key(self, manager):
        manager.set("temperature", 0.3)
        data = json.loads(manager.config_file.read_text())
        assert data == {"temperature": 0.3}

    def test_defaults_are_not_frozen(self, manager):
        """The web_tools_in_main bug class: defaults must follow code changes."""
        manager.set("default_model", "some-model")
        data = json.loads(manager.config_file.read_text())
        assert "web_tools_in_main" not in data
        assert "max_iterations" not in data
        assert "log_level" not in data

    def test_explicit_file_keys_survive_resave(self, manager):
        manager.config_file.write_text(json.dumps({"temperature": 0.2}))
        manager.load()
        manager.set("default_model", "m")
        data = json.loads(manager.config_file.read_text())
        assert data["temperature"] == 0.2
        assert data["default_model"] == "m"

    def test_direct_mutation_still_persists(self, manager):
        """Covers callers that mutate _config and call save() directly."""
        manager._config = Config(default_model="gpt-x")
        manager.save()
        data = json.loads(manager.config_file.read_text())
        assert data["default_model"] == "gpt-x"

    def test_env_values_not_frozen_to_disk(self, manager, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-0123456789")
        manager.load()
        manager.set("temperature", 0.1)
        data = json.loads(manager.config_file.read_text())
        assert "anthropic_api_key" not in data

    def test_reset_clears_tracking(self, manager):
        manager.set("temperature", 0.3)
        manager.reset()
        assert manager._explicit_keys == set()
        assert not manager.config_file.exists()
        manager._config = None
        # A later set() starts from a clean slate
        manager.set("default_model", "m")
        data = json.loads(manager.config_file.read_text())
        assert "temperature" not in data


class TestDefaultWins:
    def test_changed_code_default_applies_to_existing_users(self, manager):
        """Simulate: user set one key long ago; code default changes later."""
        manager.set("temperature", 0.3)
        # New process: reload from disk
        manager._config = None
        cfg = manager.load()
        # Defaults still come from code, not from a frozen file snapshot
        assert cfg.web_tools_in_main is Config().web_tools_in_main
        assert cfg.temperature == 0.3


class TestExecutionConfigKeys:
    """Env coercion / persistence / project overlay for the timeout, retry,
    and background-job keys added by the execution-hardening work."""

    def test_env_values_coerced_to_declared_types(self, manager, monkeypatch):
        monkeypatch.setenv("CODERAI_TOOL_TIMEOUT_SECONDS", "45.5")
        monkeypatch.setenv("CODERAI_SUBPROCESS_TIMEOUT_SECONDS", "90")
        monkeypatch.setenv("CODERAI_TOOL_RETRY_MAX_ATTEMPTS", "3")
        monkeypatch.setenv("CODERAI_TOOL_RETRY_BASE_DELAY", "0.5")
        monkeypatch.setenv("CODERAI_MAX_BACKGROUND_JOBS", "5")
        monkeypatch.setenv("CODERAI_MAX_BACKGROUND_PROCESSES", "12")
        cfg = manager.load()
        assert cfg.tool_timeout_seconds == 45.5
        assert cfg.subprocess_timeout_seconds == 90.0
        assert cfg.tool_retry_max_attempts == 3
        assert cfg.tool_retry_base_delay == 0.5
        assert cfg.max_background_jobs == 5
        assert cfg.max_background_processes == 12

    def test_garbage_env_value_ignored(self, manager, monkeypatch):
        monkeypatch.setenv("CODERAI_MAX_BACKGROUND_JOBS", "lots")
        cfg = manager.load()
        assert cfg.max_background_jobs == Config().max_background_jobs

    def test_env_derived_keys_never_persisted(self, manager, monkeypatch):
        monkeypatch.setenv("CODERAI_TOOL_TIMEOUT_SECONDS", "45.5")
        monkeypatch.setenv("CODERAI_MAX_BACKGROUND_JOBS", "5")
        manager.load()
        manager.set("temperature", 0.1)
        data = json.loads(manager.config_file.read_text())
        assert "tool_timeout_seconds" not in data
        assert "max_background_jobs" not in data

    def test_project_overlay_applies_timeout_keys(self, manager, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".coderAI").mkdir(parents=True)
        (proj / ".coderAI" / "config.json").write_text(
            json.dumps(
                {
                    "tool_timeout_seconds": 240,
                    "tool_timeout_overrides": {"run_tests": 900},
                }
            )
        )
        cfg = manager.load_project_config(str(proj))
        assert cfg.tool_timeout_seconds == 240.0
        assert cfg.tool_timeout_overrides == {"run_tests": 900}

    def test_project_overlay_cannot_raise_host_resource_caps(self, manager, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".coderAI").mkdir(parents=True)
        (proj / ".coderAI" / "config.json").write_text(
            json.dumps({"max_background_jobs": 16, "max_background_processes": 64})
        )
        cfg = manager.load_project_config(str(proj))
        assert cfg.max_background_jobs == Config().max_background_jobs
        assert cfg.max_background_processes == Config().max_background_processes


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
class TestPermissions:
    def test_config_dir_created_0700(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        m = ConfigManager()
        assert (m.config_dir.stat().st_mode & 0o777) == 0o700
