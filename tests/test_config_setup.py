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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(home / ".coderai"))
    monkeypatch.setattr("coderai.config._home", lambda: home)
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
    from coderai.config import (
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
    from coderai.exception import ConfigError
    from coderai.config import load_typed_config_from_string

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
    from coderai.share import get_share_dir
    from coderai.config import get_config_file, load_typed_config

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
    from coderai.share import get_share_dir

    target = tmp_path / "custom-share"
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(target))
    assert get_share_dir() == target
    assert target.is_dir()


def test_config_atomic_write_leaves_no_tmp(tmp_path: pathlib.Path) -> None:
    """Atomic JSON writes land fully formed with no temp files left behind."""
    from coderai.utils.io import atomic_json_write

    target = tmp_path / "sub" / "data.json"
    atomic_json_write({"a": [1, 2, 3]}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": [1, 2, 3]}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_config_settings_overlay_resolves_model_and_client(isolated_home: pathlib.Path) -> None:
    """Saved typed config resolves to settings and an offline client dict."""
    from coderai.llm import create_openai_client
    from coderai.config import resolve_current_settings
    from coderai.config import LLMModel, LLMProvider, TypedConfig, save_typed_config
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
    from coderai.config import resolve_current_settings

    resolved = resolve_current_settings(str(isolated_home))
    assert resolved["model"] == "gpt-6-luna"
    assert resolved["providerType"] == "openai_legacy"


def test_config_save_provider_key_user_scope_persists(fake_home: pathlib.Path) -> None:
    """Saving a provider key persists it to user settings and the env."""
    from coderai.config import mask_api_key, read_settings, save_provider_api_key

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
    from coderai.ui.shell.app import _build_parser
    from coderai.ui.shell.setup import run_setup_cli
    from coderai.config import read_settings

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
    from coderai.ui.shell.app import _build_parser
    from coderai.ui.shell.setup import run_setup_cli

    assert run_setup_cli(_build_parser().parse_args(["--status"]), project_root=str(tmp_path)) == 0


def test_config_setup_cli_positional_setup_dispatches_wizard(
    tmp_path: pathlib.Path, fake_home: pathlib.Path
) -> None:
    """Bare `setup` prompt dispatches to the setup wizard CLI."""
    from coderai.ui.shell.app import main

    with patch("coderai.ui.shell.setup.run_setup_cli", return_value=0) as mock_setup:
        assert main(["setup"]) == 0
        assert mock_setup.called


def test_config_probe_succeeds_with_mocked_client() -> None:
    """Connectivity probe reports success when the mocked client answers."""
    from coderai.llm import probe_provider_connectivity

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
    from coderai.llm import probe_provider_connectivity

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODERAI_API_KEY", raising=False)
    success, _msg = probe_provider_connectivity(
        model="gpt-5.6-sol", base_url="https://api.openai.com/v1", api_key=None
    )
    assert success is False


def test_setup_wizard_cancel_leaves_settings_untouched(fake_home: pathlib.Path) -> None:
    """Cancelling the save-scope prompt configures nothing and writes nothing."""
    from coderai.ui.shell.setup import prompt_save_scope
    from coderai.config import get_configured_provider_keys

    with patch("coderai.ui.shell.setup.select_with_arrows", return_value=None):
        assert prompt_save_scope() is None
    status_map = get_configured_provider_keys()
    assert status_map
    assert all(info["configured"] is False for info in status_map.values())
    assert list(fake_home.rglob("settings*")) == []


def test_config_model_picker_cancel_keeps_current() -> None:
    """Cancelling the model picker keeps the current model unchanged."""
    from coderai.ui.shell.session_picker import select_model_interactive

    with patch("coderai.ui.shell.session_picker.select_with_arrows", return_value=None):
        assert select_model_interactive(None, "gpt-5.6-terra") == "gpt-5.6-terra"


def test_config_model_picker_number_selects_curated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Numeric model-picker input selects the corresponding curated model."""
    from coderai.ui.shell.session_picker import select_model_interactive

    monkeypatch.setattr("builtins.input", lambda _: "1")
    assert select_model_interactive(None, "gpt-6-luna") == "gpt-6-astra"


def test_typed_config_normalizes_wire_model_and_allows_curated() -> None:
    """TypedConfig normalizes wire model names to configured keys and allows external models."""
    from coderai.config import LLMModel, LLMProvider, TypedConfig
    from pydantic import SecretStr

    # 1. Normalizes wire model name to matching key
    cfg = TypedConfig(
        default_model="kimi-for-coding",
        providers={
            "managed:kimi-code": LLMProvider(
                type="kimi", base_url="https://api.kimi.com/coding/v1", api_key=SecretStr("")
            )
        },
        models={
            "kimi-code/kimi-for-coding": LLMModel(
                provider="managed:kimi-code", model="kimi-for-coding", max_context_size=262144
            )
        },
    )
    assert cfg.default_model == "kimi-code/kimi-for-coding"

    # 2. Allows unconfigured standard curated models without error
    cfg2 = TypedConfig(
        default_model="gpt-5.6-luna",
        providers={
            "managed:kimi-code": LLMProvider(
                type="kimi", base_url="https://api.kimi.com/coding/v1", api_key=SecretStr("")
            )
        },
        models={
            "kimi-code/kimi-for-coding": LLMModel(
                provider="managed:kimi-code", model="kimi-for-coding", max_context_size=262144
            )
        },
    )
    assert cfg2.default_model == "gpt-5.6-luna"


def test_typed_config_rejects_missing_provider() -> None:
    """TypedConfig still enforces that models must point to a declared provider."""
    from coderai.config import LLMModel, TypedConfig
    import pytest

    with pytest.raises(ValueError, match="not found in providers"):
        TypedConfig(
            models={
                "custom": LLMModel(provider="nonexistent", model="gpt-4", max_context_size=64000)
            },
            providers={},
        )


def test_create_openai_client_resilient_to_typed_config_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_openai_client does not raise UnboundLocalError when load_typed_config fails."""
    from coderai.llm import create_openai_client

    def mock_broken_load():
        raise RuntimeError("simulated config failure")

    monkeypatch.setattr("coderai.config.load_typed_config", mock_broken_load)
    info = create_openai_client()
    assert info is not None
    assert "model" in info
    assert info["model"] == "gpt-5.6-luna" or info.get("displayModel")


def test_resolve_model_provider_routing_kimi_prefers_oauth_over_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing for Kimi models prioritizes OAuth token over generic sk-proj OpenAI keys."""
    from coderai.llm import resolve_model_provider_routing
    from coderai.auth.oauth import OAuthToken

    mock_tok = OAuthToken(
        access_token="kimi_test_access_token",
        refresh_token="rf",
        expires_at=9999999999.0,
        scope="kimi-code",
        token_type="Bearer",
        expires_in=900.0,
    )
    monkeypatch.setattr("coderai.auth.oauth.load_token", lambda _: mock_tok)

    base_url, api_key = resolve_model_provider_routing(
        model="kimi-for-coding",
        explicit_api_key="sk-proj-openai-key-that-should-not-be-sent-to-kimi",
    )
    assert base_url == "https://api.kimi.com/coding/v1"
    assert api_key == "kimi_test_access_token"


def test_test_api_connection_handles_403_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_provider_connectivity returns clear quota message on HTTP 403 limit errors."""
    from coderai.llm import probe_provider_connectivity

    class MockPermissionDeniedError(Exception):
        pass

    def mock_create(*args, **kwargs):
        raise MockPermissionDeniedError(
            "Error code: 403 - You've reached your monthly usage limit for this billing cycle."
        )

    class MockChat:
        completions = type("Comp", (), {"create": staticmethod(mock_create)})()

    class MockOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = MockChat()

    monkeypatch.setattr("openai.OpenAI", MockOpenAI)
    success, msg = probe_provider_connectivity(
        model="kimi-for-coding", api_key="tok", base_url="https://api.kimi.com/coding/v1"
    )
    assert success is False
    assert "Quota Exceeded (403)" in msg
