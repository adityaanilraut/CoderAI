import pytest

from coderAI.llm.anthropic import AnthropicProvider, MODEL_ALIASES
from coderAI.llm.deepseek import DeepSeekProvider
from coderAI.llm.factory import get_models_by_provider
from coderAI.llm.gemini import GeminiProvider
from coderAI.llm.groq import GroqProvider
from coderAI.llm.meta import MetaProvider
from coderAI.llm.openai import OpenAIProvider


def test_current_openai_alias_and_context_limit():
    provider = OpenAIProvider("gpt-5.6", api_key="test")
    assert provider.actual_model == "gpt-5.6-sol"
    assert provider.model_context_window == 1_050_000
    assert provider.get_effective_context_window() == 1_050_000


def test_current_anthropic_alias_and_context_limit():
    provider = AnthropicProvider("opus", api_key="test")
    assert provider.actual_model == "claude-opus-4-8"
    assert provider.model_context_window == 1_000_000
    assert AnthropicProvider("fable", api_key="test").actual_model == "claude-fable-5"
    assert AnthropicProvider("sonnet", api_key="test").actual_model == "claude-sonnet-5"


def test_current_gemini_context_limit():
    provider = GeminiProvider("gemini-3.5-flash", api_key="test")
    assert provider.model_context_window == 1_048_576
    assert provider.get_effective_context_window() == 1_048_576


@pytest.mark.parametrize(
    "provider_class",
    [OpenAIProvider, GeminiProvider, GroqProvider, DeepSeekProvider, MetaProvider],
)
def test_every_listed_cloud_model_has_a_context_limit(provider_class):
    canonical_models = set(provider_class.SUPPORTED_MODELS.values())
    assert canonical_models <= set(provider_class.MODEL_CONTEXT_WINDOWS)


def test_every_anthropic_alias_has_a_context_limit():
    assert set(MODEL_ALIASES.values()) <= set(AnthropicProvider.MODEL_CONTEXT_WINDOWS)


def test_current_models_are_user_visible():
    listings = {label: models for label, models, _requirement in get_models_by_provider()}
    assert "gpt-5.6" in listings["OpenAI Provider"]
    assert "claude-4.8-opus" in listings["Anthropic Provider"]
    assert "claude-5-sonnet" in listings["Anthropic Provider"]
    assert "claude-5-fable" in listings["Anthropic Provider"]


def test_unknown_context_limit_uses_safe_fallback():
    provider = AnthropicProvider("claude-unknown", api_key="test")
    assert provider.model_context_window is None
    assert provider.get_effective_context_window(96_000) == 96_000


def test_per_call_reasoning_override_disables_openai_and_deepseek():
    openai = OpenAIProvider("gpt-5.6", api_key="test", reasoning_effort="high")
    deepseek = DeepSeekProvider("deepseek-v4-pro", api_key="test", reasoning_effort="high")

    openai_params = openai._build_request_params(
        [{"role": "user", "content": "x"}], reasoning_effort="none"
    )
    deepseek_params = deepseek._build_request_params(
        [{"role": "user", "content": "x"}], reasoning_effort="none"
    )

    assert "reasoning_effort" not in openai_params
    assert deepseek_params["extra_body"] == {"thinking": {"type": "disabled"}}


def test_gpt56_forces_none_reasoning_effort_when_tools_present():
    """gpt-5.6-sol rejects tools + non-none reasoning on /v1/chat/completions."""
    provider = OpenAIProvider("gpt-5.6", api_key="test", reasoning_effort="high")
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    with_tools = provider._build_request_params([{"role": "user", "content": "x"}], tools=tools)
    without_tools = provider._build_request_params([{"role": "user", "content": "x"}])

    assert with_tools["reasoning_effort"] == "none"
    assert without_tools["reasoning_effort"] == "high"


def test_gpt54_mini_omits_reasoning_effort():
    provider = OpenAIProvider("gpt-5.4-mini", api_key="test", reasoning_effort="high")
    params = provider._build_request_params(
        [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "ping", "parameters": {}}}],
    )
    assert "reasoning_effort" not in params


def test_non_anthropic_provider_strips_private_provider_state():
    provider = OpenAIProvider("gpt-5.6", api_key="test")
    cleaned = provider.clean_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "private summary",
                "tool_calls": [
                    {
                        "id": "tool_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                        "provider_state": {"provider": "anthropic"},
                    }
                ],
            }
        ]
    )

    assert "reasoning_content" not in cleaned[0]
    assert "provider_state" not in cleaned[0]["tool_calls"][0]
