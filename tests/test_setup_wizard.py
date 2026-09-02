"""Unit tests for CoderAI /setup wizard, CLI configuration, and provider key management."""

import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from coderai.cli.app import _build_parser, main
from coderai.cli.commands import parse_slash_command, resolve_command
from coderai.cli.completer import CoderAICompleter
from coderai.cli.setup_wizard import (
    configure_provider_key_interactive,
    run_quick_setup_wizard,
    run_setup_cli,
    run_setup_wizard,
    select_and_save_model_interactive,
)
from coderai.core.openai_client import (
    probe_provider_connectivity,
)
from coderai.core.settings import (
    get_configured_provider_keys,
    mask_api_key,
    read_project_settings,
    read_settings,
    save_active_model_setting,
    save_base_url_setting,
    save_custom_endpoint_config,
    save_provider_api_key,
)


def test_mask_api_key():
    assert mask_api_key(None) == "Not Set"
    assert mask_api_key("") == "Not Set"
    assert mask_api_key("12345") == "****"
    assert mask_api_key("sk-proj-123456789xyz") == "sk-p...xyz"
    assert mask_api_key("AIzaSyD987654321") == "AIza...321"


def test_save_provider_api_key_user_scope(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    # Save OpenAI key
    var_name = save_provider_api_key("openai", "sk-test-openai-123456", scope="user")
    assert var_name == "OPENAI_API_KEY"
    assert os.environ.get("OPENAI_API_KEY") == "sk-test-openai-123456"

    user_settings = read_settings()
    assert user_settings is not None
    assert user_settings.get("apiKey") == "sk-test-openai-123456"
    assert user_settings.get("env", {}).get("OPENAI_API_KEY") == "sk-test-openai-123456"

    # Save DeepSeek key
    var_ds = save_provider_api_key("deepseek", "sk-deepseek-998877", scope="user")
    assert var_ds == "DEEPSEEK_API_KEY"
    assert os.environ.get("DEEPSEEK_API_KEY") == "sk-deepseek-998877"

    user_settings = read_settings()
    assert user_settings.get("env", {}).get("DEEPSEEK_API_KEY") == "sk-deepseek-998877"


def test_save_provider_api_key_project_scope(tmp_path: pathlib.Path):
    proj_root = str(tmp_path)
    var_gemini = save_provider_api_key(
        "gemini", "AIzaSyFakeKey123", scope="project", project_root=proj_root
    )
    assert var_gemini == "GEMINI_API_KEY"
    assert os.environ.get("GEMINI_API_KEY") == "AIzaSyFakeKey123"

    proj_settings = read_project_settings(proj_root)
    assert proj_settings is not None
    assert proj_settings.get("env", {}).get("GEMINI_API_KEY") == "AIzaSyFakeKey123"


def test_save_active_model_and_base_url(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    save_active_model_setting("deepseek-v4-pro", scope="user")
    assert os.environ.get("CODERAI_MODEL") == "deepseek-v4-pro"
    user_settings = read_settings()
    assert user_settings.get("model") == "deepseek-v4-pro"

    save_base_url_setting("https://custom.llm.proxy/v1", scope="user")
    assert os.environ.get("CODERAI_BASE_URL") == "https://custom.llm.proxy/v1"
    user_settings = read_settings()
    assert user_settings.get("baseURL") == "https://custom.llm.proxy/v1"


def test_save_custom_endpoint_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    save_custom_endpoint_config(
        provider_name="ollama",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        default_model="qwen2.5-coder:32b",
        scope="user",
    )

    user_settings = read_settings()
    assert user_settings.get("baseURL") == "http://localhost:11434/v1"
    assert user_settings.get("apiKey") == "ollama"
    assert user_settings.get("model") == "qwen2.5-coder:32b"


def test_get_configured_provider_keys(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-123456789")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-987654321")

    status_map = get_configured_provider_keys(str(tmp_path))
    assert "openai" in status_map
    assert "deepseek" in status_map
    assert "gemini" in status_map
    assert "anthropic" in status_map
    assert "openrouter" in status_map

    assert status_map["openai"]["configured"] is True
    assert "sk-o...321" in status_map["openai"]["masked_key"]
    assert status_map["deepseek"]["configured"] is True
    assert "sk-d...789" in status_map["deepseek"]["masked_key"]


def test_slash_command_setup_resolution():
    cmd = resolve_command("setup")
    assert cmd is not None
    assert cmd.name == "setup"
    assert "auth" in cmd.aliases
    assert "keys" in cmd.aliases
    assert "configure" in cmd.aliases
    assert "keys" in cmd.subcommands
    assert "models" in cmd.subcommands
    assert "test" in cmd.subcommands
    assert "status" in cmd.subcommands

    c, arg = parse_slash_command("/setup keys")
    assert c == "/setup"
    assert arg == "keys"

    c2, arg2 = parse_slash_command("/keys")
    assert c2 == "/setup"


def test_completer_slash_command_setup(tmp_path: pathlib.Path):
    completer = CoderAICompleter(str(tmp_path))
    res = completer.complete("/set", 0)
    assert res is not None
    assert "/setup" in res


def test_cli_parser_setup_options():
    parser = _build_parser()
    args = parser.parse_args(["--setup"])
    assert args.setup is True

    args_prov = parser.parse_args(
        ["--provider", "deepseek", "--key", "sk-123", "--setup-model", "deepseek-v4-pro"]
    )
    assert args_prov.setup_provider == "deepseek"
    assert args_prov.setup_key == "sk-123"
    assert args_prov.setup_model == "deepseek-v4-pro"

    args_test = parser.parse_args(["--test"])
    assert args_test.setup_test is True

    args_status = parser.parse_args(["--status"])
    assert args_status.setup_status is True


def test_run_setup_cli_non_interactive_key(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    parser = _build_parser()
    args = parser.parse_args(
        ["--provider", "openai", "--key", "sk-test-cli-key-123", "--setup-model", "gpt-5.6-sol"]
    )

    ret = run_setup_cli(args, project_root=str(tmp_path))
    assert ret == 0

    user_settings = read_settings()
    assert user_settings.get("apiKey") == "sk-test-cli-key-123"
    assert user_settings.get("model") == "gpt-5.6-sol"


def test_run_setup_cli_non_interactive_base_url(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    parser = _build_parser()
    args = parser.parse_args(
        [
            "--base-url",
            "http://localhost:11434/v1",
            "--setup-model",
            "qwen2.5-coder:32b",
            "--provider",
            "ollama",
        ]
    )

    ret = run_setup_cli(args, project_root=str(tmp_path))
    assert ret == 0

    user_settings = read_settings()
    assert user_settings.get("baseURL") == "http://localhost:11434/v1"
    assert user_settings.get("model") == "qwen2.5-coder:32b"


def test_run_setup_cli_status(tmp_path: pathlib.Path):
    parser = _build_parser()
    args = parser.parse_args(["--status"])
    ret = run_setup_cli(args, project_root=str(tmp_path))
    assert ret == 0


def test_probe_provider_connectivity_mock_success(monkeypatch: pytest.MonkeyPatch):
    mock_resp = MagicMock()
    mock_resp.model = "gpt-5.6-sol"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("openai.OpenAI", return_value=mock_client):
        success, msg = probe_provider_connectivity(
            model="gpt-5.6-sol",
            base_url="https://api.openai.com/v1",
            api_key="sk-fake-key",
        )
        assert success is True
        assert "Successfully connected" in msg


def test_probe_provider_connectivity_no_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODERAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    success, msg = probe_provider_connectivity(
        model="gpt-5.6-sol",
        base_url="https://api.openai.com/v1",
        api_key=None,
    )
    assert success is False
    assert "No API key" in msg


def test_interactive_setup_wizard_subcommands(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    console = MagicMock()

    # /setup status
    run_setup_wizard(console, project_root=str(tmp_path), initial_subcommand="status")

    # /setup test
    with patch(
        "coderai.cli.setup_wizard.probe_provider_connectivity",
        return_value=(True, "Mock connection ok"),
    ):
        run_setup_wizard(console, project_root=str(tmp_path), initial_subcommand="test")


def test_main_cli_positional_setup_dispatch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    with patch("coderai.cli.setup_wizard.run_setup_cli", return_value=0) as mock_setup:
        ret = main(["setup"])
        assert ret == 0
        assert mock_setup.called


def test_select_with_arrows_cancel(monkeypatch: pytest.MonkeyPatch):
    from coderai.cli.interactive_menu import select_with_arrows

    # Test non-TTY input 'q' / 'cancel' with allow_cancel=True
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "q")
    res = select_with_arrows(None, [("a", "A", "desc A")], allow_cancel=True)
    assert res is None

    monkeypatch.setattr("builtins.input", lambda prompt="": "cancel")
    res2 = select_with_arrows(None, [("a", "A", "desc A")], allow_cancel=True)
    assert res2 is None


def test_read_input_cancel():
    from coderai.cli.setup_wizard import _read_input

    with patch("builtins.input", return_value="cancel"):
        assert _read_input("Enter key") is None

    with patch("builtins.input", return_value="q"):
        assert _read_input("Enter key") is None

    with patch("builtins.input", return_value="sk-valid-key"):
        assert _read_input("Enter key") == "sk-valid-key"


def test_prompt_save_scope_cancel():
    from coderai.cli.setup_wizard import prompt_save_scope

    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=None):
        assert prompt_save_scope() is None

    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=2):
        assert prompt_save_scope() is None


def test_configure_provider_key_cancel():

    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=None):
        res = configure_provider_key_interactive(None)
        assert res is None


def test_select_and_save_model_cancel(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr("coderai.core.settings._home", lambda: fake_home)

    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=None):
        chosen = select_and_save_model_interactive(None, project_root=str(tmp_path))
        assert chosen == "gpt-5.6-luna" or chosen is not None


def test_run_quick_setup_wizard_cancel():
    with patch("coderai.cli.setup_wizard.select_with_arrows", return_value=None):
        # Should cleanly exit without raising exception
        run_quick_setup_wizard(None)


def test_run_setup_wizard_exit_option():
    console = MagicMock()
    # Selecting the last item (exit option) or None (Esc/q)
    with patch("coderai.cli.setup_wizard.select_with_arrows", side_effect=[None]):
        run_setup_wizard(console)
