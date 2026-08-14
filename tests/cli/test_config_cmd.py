"""Flat CLI compatibility over the nested Config representation."""

import json

from click.testing import CliRunner

from coderAI.cli.config_cmd import config
from coderAI.system.config import config_manager


def _isolate_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_manager, "config_dir", tmp_path)
    monkeypatch.setattr(config_manager, "config_file", tmp_path / "config.json")
    monkeypatch.setattr(config_manager, "_config", None)
    monkeypatch.setattr(config_manager, "_explicit_keys", set())
    monkeypatch.setattr(config_manager, "_env_keys", set())
    monkeypatch.setattr(config_manager, "_file_values", {})


def test_flat_config_set_command_persists_nested_representation(tmp_path, monkeypatch) -> None:
    _isolate_config(tmp_path, monkeypatch)

    result = CliRunner().invoke(config, ["set", "temperature", "0.3"])

    assert result.exit_code == 0, result.output
    assert config_manager.get("temperature") == 0.3
    assert json.loads(config_manager.config_file.read_text()) == {
        "providers": {"temperature": 0.3}
    }


def test_dotted_config_set_and_flat_get_are_compatible(tmp_path, monkeypatch) -> None:
    _isolate_config(tmp_path, monkeypatch)

    result = CliRunner().invoke(config, ["set", "tools.tool_timeout_seconds", "45"])

    assert result.exit_code == 0, result.output
    assert config_manager.get("tool_timeout_seconds") == 45.0
    assert config_manager.get("tools.tool_timeout_seconds") == 45.0
