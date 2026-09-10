"""Consolidated config + setup tests; offline only (probe runs on a mock client)."""

from __future__ import annotations

import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def isolated_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Point HOME at a temp dir and strip env so the real config never leaks."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "CODERAI_SHARE_DIR",
        "CODERAI_CONFIG_FILE",
        "CODERAI_CONFIG_STRING",
        "CODERAI_MODEL",
        "CODERAI_API_KEY",
        "CODERAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def fake_home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Redirect settings home at a temp dir and strip credential env vars."""
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: home)
    for var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY",
        "CODERAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


VALID_TOML = """
default_model = "main"
[providers.main]
type = "openai_legacy"
base_url = "https://api.openai.com/v1"
api_key = "sk-test"
[models.main]
provider = "main"
model = "gpt-x"
max_context_size = 128000
"""


def test_config_typed_round_trip_saves_and_reloads(isolated_home: pathlib.Path) -> None:
    """A parsed typed config survives a save/load round-trip with defaults."""
    from coderai.core.typed_config import (
        get_config_file,
        load_typed_config,
        load_typed_config_from_string,
        save_typed_config,
    )

    cfg = load_typed_config_from_string(VALID_TOML)
    assert cfg.default_model == "main"
    assert cfg.models["main"].max_context_size == 128000
    assert cfg.loop_control.max_steps_per_turn == 1000
    assert cfg.is_from_default_location is False
    save_typed_config(cfg)
    assert get_config_file().is_file()
    reloaded = load_typed_config()
    assert reloaded.default_model == "main"
    assert reloaded.is_from_default_location is True


def test_config_json_string_parses_and_rejects_invalid() -> None:
    """JSON config strings parse; malformed or empty input raises."""
    from coderai.core.errors import ConfigError
    from coderai.core.typed_config import load_typed_config_from_string

    payload = json.dumps(
        {
            "default_model": "m",
            "providers": {"p": {"type": "kimi", "base_url": "https://x", "api_key": "k"}},
            "models": {"m": {"provider": "p", "model": "kimi-k2", "max_context_size": 1000}},
        }
    )
    assert load_typed_config_from_string(payload).providers["p"].type == "kimi"
    with pytest.raises(ConfigError):
        load_typed_config_from_string("not valid {{{")
    with pytest.raises(ConfigError):
        load_typed_config_from_string("")


def test_config_legacy_settings_migrate_to_typed(isolated_home: pathlib.Path) -> None:
    """A legacy share/settings.json migrates into the typed config on load."""
    from coderai.core.share import get_share_dir
    from coderai.core.typed_config import get_config_file, load_typed_config

    get_share_dir().joinpath("settings.json").write_text(
        json.dumps({"model": "gpt-x", "baseURL": "https://x.test/v1", "apiKey": "sk-legacy"})
    )
    assert not get_config_file().exists()
    cfg = load_typed_config()
    assert cfg.default_model == "default"
    assert cfg.models["default"].model == "gpt-x"
    assert get_config_file().is_file()


def test_config_share_dir_env_override_creates_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CODERAI_SHARE_DIR redirects the share dir and creates it."""
    from coderai.core.share import get_share_dir

    target = tmp_path / "custom-share"
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(target))
    assert get_share_dir() == target
    assert target.is_dir()


def test_config_atomic_write_leaves_no_tmp(tmp_path: pathlib.Path) -> None:
    """Atomic JSON writes land fully formed with no temp files left behind."""
    from coderai.core.common.atomic import atomic_json_write

    target = tmp_path / "sub" / "data.json"
    atomic_json_write({"a": [1, 2, 3]}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": [1, 2, 3]}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_config_settings_overlay_resolves_model_and_client(isolated_home: pathlib.Path) -> None:
    """Saved typed config resolves to settings and an offline client dict."""
    from coderai.core.openai_client import create_openai_client
    from coderai.core.settings import resolve_current_settings
    from coderai.core.typed_config import LLMModel, LLMProvider, TypedConfig, save_typed_config
    from pydantic import SecretStr

    cfg = TypedConfig()
    cfg.providers["main"] = LLMProvider(
        type="openai_legacy", base_url="https://x.test/v1", api_key=SecretStr("sk-t")
    )
    cfg.models["main"] = LLMModel(provider="main", model="gpt-x", max_context_size=64000)
    cfg.default_model = "main"
    save_typed_config(cfg)
    resolved = resolve_current_settings(str(isolated_home))
    assert resolved["model"] == "gpt-x"
    assert resolved["providerType"] == "openai_legacy"
    client = create_openai_client(str(isolated_home))
    assert client["model"] == "gpt-x"
    assert client["contextWindow"] == 64000


def test_config_settings_legacy_fallback_returns_default_model(isolated_home: pathlib.Path) -> None:
    """With no config on disk, settings fall back to the built-in default."""
    from coderai.core.settings import resolve_current_settings

    resolved = resolve_current_settings(str(isolated_home))
    assert resolved["model"] == "gpt-5.6-luna"
    assert resolved["providerType"] == "openai_legacy"


def test_config_save_provider_key_user_scope_persists(fake_home: pathlib.Path) -> None:
    """Saving a provider key persists it to user settings and the env."""
    from coderai.core.settings import mask_api_key, read_settings, save_provider_api_key

    try:
        assert (
            save_provider_api_key("openai", "sk-test-openai-123456", scope="user")
            == "OPENAI_API_KEY"
        )
        assert os.environ.get("OPENAI_API_KEY") == "sk-test-openai-123456"
        assert read_settings().get("apiKey") == "sk-test-openai-123456"
        assert mask_api_key("sk-test-openai-123456") == "sk-t...456"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)


def test_config_setup_cli_key_saves_model_and_key(
    tmp_path: pathlib.Path, fake_home: pathlib.Path
) -> None:
    """Non-interactive --provider/--key/--setup-model run saves settings."""
    from coderai.cli.app import _build_parser
    from coderai.cli.setup_wizard import run_setup_cli
    from coderai.core.settings import read_settings

    try:
        args = _build_parser().parse_args(
            ["--provider", "openai", "--key", "sk-test-cli-key-123", "--setup-model", "gpt-5.6-sol"]
        )
        assert run_setup_cli(args, project_root=str(tmp_path)) == 0
        user_settings = read_settings()
        assert user_settings.get("apiKey") == "sk-test-cli-key-123"
        assert user_settings.get("model") == "gpt-5.6-sol"
    finally:
        os.environ.pop("OPENAI_API_KEY", None)


def test_config_setup_cli_status_returns_zero(tmp_path: pathlib.Path) -> None:
    """--status reports provider state and exits zero without network."""
    from coderai.cli.app import _build_parser
    from coderai.cli.setup_wizard import run_setup_cli

    assert run_setup_cli(_build_parser().parse_args(["--status"]), project_root=str(tmp_path)) == 0


def test_config_setup_cli_positional_setup_dispatches_wizard(
    tmp_path: pathlib.Path, fake_home: pathlib.Path
) -> None:
    """Bare `setup` prompt dispatches to the setup wizard CLI."""
    from coderai.cli.app import main

    with patch("coderai.cli.setup_wizard.run_setup_cli", return_value=0) as mock_setup:
        assert main(["setup"]) == 0
        assert mock_setup.called


def test_config_probe_succeeds_with_mocked_client() -> None:
    """Connectivity probe reports success when the mocked client answers."""
    from coderai.core.openai_client import probe_provider_connectivity

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(model="gpt-5.6-sol")
    with patch("openai.OpenAI", return_value=mock_client):
        success, msg = probe_provider_connectivity(
            model="gpt-5.6-sol", base_url="https://api.openai.com/v1", api_key="sk-fake-key"
        )
        assert success is True
        assert "Successfully connected" in msg


def test_config_probe_fails_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connectivity probe fails cleanly when no API key is configured."""
    from coderai.core.openai_client import probe_provider_connectivity

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODERAI_API_KEY", raising=False)
    success, _msg = probe_provider_connectivity(
        model="gpt-5.6-sol", base_url="https://api.openai.com/v1", api_key=None
    )
    assert success is False


def test_setup_wizard_cancel_leaves_settings_untouched(fake_home: pathlib.Path) -> None:
    """Cancelling the save-scope prompt configures nothing and writes nothing."""
    from coderai.cli.setup_wizard import prompt_save_scope
    from coderai.core.settings import get_configured_provider_keys

    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=None):
        assert prompt_save_scope() is None
    status_map = get_configured_provider_keys()
    assert status_map
    assert all(info["configured"] is False for info in status_map.values())
    assert list(fake_home.rglob("settings*")) == []


def test_config_model_picker_cancel_keeps_current() -> None:
    """Cancelling the model picker keeps the current model unchanged."""
    from coderai.cli.interactive_menu import select_model_interactive

    with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=None):
        assert select_model_interactive(None, "gpt-5.6-terra") == "gpt-5.6-terra"


def test_config_model_picker_number_selects_curated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric model-picker input selects the corresponding curated model."""
    from coderai.cli.interactive_menu import select_model_interactive

    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert select_model_interactive(None, "gpt-5.6-luna") == "gpt-5.6-sol"
