"""Focused regressions for centralized credential redaction."""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from coderAI.cli.config_cmd import config
from coderAI.cli.main import cli
from coderAI.system.config import Config, ConfigManager
from coderAI.system.redaction import REDACTED, redact_secrets, redact_text


PROVIDER_CANARIES = {
    "openai_api_key": "sk-proj-OpenAICanary0123456789",
    "anthropic_api_key": "sk-ant-api03-AnthropicCanary0123456789",
    "groq_api_key": "gsk_GroqCanary0123456789",
    "deepseek_api_key": "sk-DeepSeekCanary0123456789",
    "gemini_api_key": "AIzaSyGeminiCanary01234567890123456789",
    "meta_api_key": "LLM|MetaCanary0123456789",
    "tavily_api_key": "tvly-dev-TavilyCanary0123456789",
    "exa_api_key": "exa-ExaCanary0123456789",
}


def test_recursive_redaction_covers_all_provider_keys_and_nested_headers() -> None:
    value = {
        **PROVIDER_CANARIES,
        "connections": [
            {
                "headers": {
                    "Authorization": "Bearer NestedAuthorizationCanary",
                    "X-Api-Key": "NestedHeaderCanary",
                },
                "client_secret": "NestedClientSecret",
            }
        ],
    }

    redacted = redact_secrets(value)

    for key in PROVIDER_CANARIES:
        assert redacted[key] == REDACTED
    headers = redacted["connections"][0]["headers"]
    assert headers == {"Authorization": REDACTED, "X-Api-Key": REDACTED}
    assert redacted["connections"][0]["client_secret"] == REDACTED


def test_provider_prefixed_and_camel_case_secret_keys_are_recognized() -> None:
    value = {
        "custom_api_key": "opaque",
        "customApiKey": "opaque",
        "github_token": "opaque",
        "accessToken": "opaque",
    }
    assert redact_secrets(value) == {key: REDACTED for key in value}


@pytest.mark.parametrize("canary", PROVIDER_CANARIES.values())
def test_provider_canaries_are_redacted_from_free_form_text(canary: str) -> None:
    output = redact_text(f"request failed using {canary}; retrying")
    assert canary not in output
    assert REDACTED in output


@pytest.mark.parametrize(
    "text",
    [
        "api_key=opaque-value",
        "token: short-token",
        '"client_secret": "json-secret"',
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "password = hunter2",
    ],
)
def test_text_key_value_patterns_are_redacted(text: str) -> None:
    output = redact_text(text)
    assert REDACTED in output
    assert output != text


def test_ordinary_hashes_model_ids_and_token_counts_are_preserved() -> None:
    sha256 = "a" * 64
    value = {
        "sha256": sha256,
        "model_id": "claude-sonnet-4-6",
        "request_id": "req_0123456789abcdef0123456789abcdef",
        "prompt_tokens": 1234,
    }
    assert redact_secrets(value) == value
    text = f"model=claude-sonnet-4-6 sha256={sha256}"
    assert redact_text(text) == text


def test_config_show_fully_redacts_all_provider_credentials(tmp_path) -> None:
    manager = ConfigManager()
    manager.config_dir = tmp_path
    manager.config_file = tmp_path / "config.json"
    manager._config = Config(**PROVIDER_CANARIES)

    shown = manager.show()

    assert all(shown[key] == REDACTED for key in PROVIDER_CANARIES)
    rendered = repr(shown)
    assert all(canary not in rendered for canary in PROVIDER_CANARIES.values())


def test_config_set_does_not_echo_secret(monkeypatch) -> None:
    canary = PROVIDER_CANARIES["openai_api_key"]
    recorded: dict[str, object] = {}

    def set_value(key: str, value: object) -> None:
        recorded[key] = value

    monkeypatch.setattr("coderAI.cli.config_cmd.config_manager.set", set_value)
    result = CliRunner().invoke(config, ["set", "openai_api_key", canary])

    assert result.exit_code == 0
    assert recorded["openai_api_key"] == canary
    assert canary not in result.output
    assert "Set openai_api_key" in result.output


def test_doctor_reports_provider_presence_without_key_fragments(tmp_path, monkeypatch) -> None:
    main_module = importlib.import_module("coderAI.cli.main")
    cfg = Config(**PROVIDER_CANARIES)
    config_file = tmp_path / "config.json"
    config_file.write_text("{}")
    monkeypatch.setattr(main_module.config_manager, "load", lambda: cfg)
    monkeypatch.setattr(main_module.config_manager, "config_dir", tmp_path)
    monkeypatch.setattr(main_module.config_manager, "config_file", config_file)
    monkeypatch.setattr(main_module.history_manager, "list_sessions", lambda: [])

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    for label in ("OpenAI", "Anthropic", "Groq", "DeepSeek", "Gemini", "Meta", "Tavily", "Exa"):
        assert f"{label}: configured" in result.output
    assert all(canary not in result.output for canary in PROVIDER_CANARIES.values())
