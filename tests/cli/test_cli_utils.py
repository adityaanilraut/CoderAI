"""Provider-specific CLI preflight regression tests."""

from coderAI.cli.utils import is_valid_model, missing_api_key_message
from coderAI.system.config import Config


def _use_config(monkeypatch, config: Config) -> None:
    monkeypatch.setattr("coderAI.system.config.config_manager.load", lambda: config)


def test_preflight_requires_the_selected_providers_key(monkeypatch):
    _use_config(monkeypatch, Config(openai_api_key="sk-openai", anthropic_api_key=None))

    error = missing_api_key_message("claude-sonnet-4-6")

    assert error is not None
    assert "Anthropic API key" in error
    assert "ANTHROPIC_API_KEY" in error


def test_preflight_accepts_selected_provider_key(monkeypatch):
    _use_config(monkeypatch, Config(anthropic_api_key="sk-anthropic"))

    assert missing_api_key_message("claude-sonnet-4-6") is None


def test_preflight_uses_default_model_when_override_absent(monkeypatch):
    _use_config(monkeypatch, Config(default_model="groq/custom", groq_api_key=None))

    error = missing_api_key_message()

    assert error is not None
    assert "Groq API key" in error


def test_prefixed_local_models_need_no_cloud_key(monkeypatch):
    _use_config(monkeypatch, Config())

    assert missing_api_key_message("ollama/qwen2.5-coder") is None
    assert missing_api_key_message("lmstudio/local-coder") is None


def test_model_validation_matches_factory_prefix_support():
    assert is_valid_model("ollama/qwen2.5-coder")
    assert is_valid_model("lmstudio/local-coder")
    assert is_valid_model("groq/custom-model")
    assert not is_valid_model("ollama/")
    assert not is_valid_model("definitely-not-a-model")
