"""Unit and integration tests for multi-model Reasoning Effort Tags (low, medium, high, max, off)."""

import pytest
from unittest.mock import MagicMock

from coderai.core.common.openai_thinking import (
    normalize_reasoning_effort,
    get_thinking_token_budget,
    build_thinking_request_options,
    GEMINI_THINKING_BUDGETS,
    ANTHROPIC_THINKING_BUDGETS,
)
from coderai.core.common.model_capabilities import (
    get_supported_reasoning_efforts,
    get_default_reasoning_effort,
    format_model_effort_tags,
    defaults_to_thinking_mode,
)
from coderai.core.session import SessionManager
from coderai.cli.interactive_menu import (
    REASONING_EFFORT_CHOICES,
)


def test_normalize_reasoning_effort():
    # Canonical levels
    assert normalize_reasoning_effort("off") == "off"
    assert normalize_reasoning_effort("minimal") == "minimal"
    assert normalize_reasoning_effort("low") == "low"
    assert normalize_reasoning_effort("medium") == "medium"
    assert normalize_reasoning_effort("high") == "high"
    assert normalize_reasoning_effort("max") == "max"

    # Aliases
    assert normalize_reasoning_effort("none") == "off"
    assert normalize_reasoning_effort("disabled") == "off"
    assert normalize_reasoning_effort("false") == "off"
    assert normalize_reasoning_effort("0") == "off"
    assert normalize_reasoning_effort("xhigh") == "max"

    # Case insensitivity and whitespace
    assert normalize_reasoning_effort("  HIGH  ") == "high"
    assert normalize_reasoning_effort("Medium") == "medium"
    assert normalize_reasoning_effort("LoW") == "low"

    # Fallback for unknown / empty
    assert normalize_reasoning_effort("") == "max"
    assert normalize_reasoning_effort(None) == "max"
    assert normalize_reasoning_effort("super-ultra-high") == "max"


def test_thinking_token_budgets():
    # Gemini token budgets
    assert get_thinking_token_budget("low", provider="gemini") == GEMINI_THINKING_BUDGETS["low"]
    assert get_thinking_token_budget("medium", provider="gemini") == GEMINI_THINKING_BUDGETS["medium"]
    assert get_thinking_token_budget("high", provider="google") == GEMINI_THINKING_BUDGETS["high"]
    assert get_thinking_token_budget("max", provider="gemini") == GEMINI_THINKING_BUDGETS["max"]
    assert get_thinking_token_budget("off", provider="gemini") == 0

    # Anthropic Claude token budgets
    assert get_thinking_token_budget("low", provider="claude") == ANTHROPIC_THINKING_BUDGETS["low"]
    assert get_thinking_token_budget("medium", provider="anthropic") == ANTHROPIC_THINKING_BUDGETS["medium"]
    assert get_thinking_token_budget("high", provider="anthropic") == ANTHROPIC_THINKING_BUDGETS["high"]
    assert get_thinking_token_budget("max", provider="claude") == ANTHROPIC_THINKING_BUDGETS["max"]
    assert get_thinking_token_budget("off", provider="anthropic") == 0


def test_openai_reasoning_wire_options():
    # OpenAI GPT-5.6 Sol / Luna / Terra & o-series
    opts_high = build_thinking_request_options(
        thinking_enabled=True, model="gpt-5.6-sol", reasoning_effort="high"
    )
    assert opts_high == {"reasoning_effort": "high"}

    opts_med = build_thinking_request_options(
        thinking_enabled=True, model="gpt-5.6-terra", reasoning_effort="medium"
    )
    assert opts_med == {"reasoning_effort": "medium"}

    opts_low = build_thinking_request_options(
        thinking_enabled=True, model="gpt-5.6-luna", reasoning_effort="low"
    )
    assert opts_low == {"reasoning_effort": "low"}

    # OpenAI maps max to high on wire because standard OpenAI API takes low/medium/high
    opts_max = build_thinking_request_options(
        thinking_enabled=True, model="o3", reasoning_effort="max"
    )
    assert opts_max == {"reasoning_effort": "high"}

    # GPT-5.6 with tools and disabled thinking sends reasoning_effort: "none"
    opts_disabled_tools = build_thinking_request_options(
        thinking_enabled=False, model="gpt-5.6-sol", has_tools=True
    )
    assert opts_disabled_tools == {"reasoning_effort": "none"}

    opts_off_tools = build_thinking_request_options(
        thinking_enabled=True, model="gpt-5.6-sol", reasoning_effort="off", has_tools=True
    )
    assert opts_off_tools == {"reasoning_effort": "none"}


def test_deepseek_wire_options():
    # DeepSeek native models take extra_body reasoning_effort tag
    opts_pro_max = build_thinking_request_options(
        thinking_enabled=True, model="deepseek-v4-pro", reasoning_effort="max"
    )
    assert opts_pro_max == {
        "extra_body": {
            "reasoning_effort": "max",
        }
    }

    opts_pro_low = build_thinking_request_options(
        thinking_enabled=True, model="deepseek-v4-pro", reasoning_effort="low"
    )
    assert opts_pro_low == {
        "extra_body": {
            "reasoning_effort": "low",
        }
    }

    opts_r1_high = build_thinking_request_options(
        thinking_enabled=True, model="deepseek-r1", reasoning_effort="high"
    )
    assert opts_r1_high == {"reasoning_effort": "high"}

    # Off / disabled DeepSeek
    opts_deepseek_off = build_thinking_request_options(
        thinking_enabled=True, model="deepseek-v4-pro", reasoning_effort="off"
    )
    assert opts_deepseek_off == {}


def test_gemini_and_generic_wire_options():
    opts_gemini_max = build_thinking_request_options(
        thinking_enabled=True, model="gemini-3.7-flash", reasoning_effort="max"
    )
    assert opts_gemini_max == {
        "extra_body": {
            "reasoning_effort": "max",
        }
    }

    opts_gemini_low = build_thinking_request_options(
        thinking_enabled=True, model="gemini-3.7-flash", reasoning_effort="low"
    )
    assert opts_gemini_low == {
        "extra_body": {
            "reasoning_effort": "low",
        }
    }

    opts_gemini_off = build_thinking_request_options(
        thinking_enabled=True, model="gemini-3.7-flash", reasoning_effort="off"
    )
    assert opts_gemini_off == {}



def test_model_capabilities_helpers():
    assert defaults_to_thinking_mode("gpt-5.6-sol") is True
    assert defaults_to_thinking_mode("deepseek-v4-pro") is True
    assert defaults_to_thinking_mode("gemini-3.7-flash") is True
    assert defaults_to_thinking_mode("claude-3-7-sonnet") is True

    # Supported efforts
    assert get_supported_reasoning_efforts("gpt-5.6-sol") == ["off", "low", "medium", "high", "max"]
    assert get_supported_reasoning_efforts("deepseek-v4-pro") == ["off", "low", "medium", "high", "max"]
    assert get_supported_reasoning_efforts("unknown-text-model") == ["off"]

    # Default reasoning effort
    assert get_default_reasoning_effort("gpt-5.6-sol") == "max"
    assert get_default_reasoning_effort("deepseek-v4-pro") == "max"
    assert get_default_reasoning_effort("gemini-3.7-flash") == "low"
    assert get_default_reasoning_effort("unknown-text-model") == "off"

    # Format tags
    tags = format_model_effort_tags("deepseek-v4-pro")
    assert "low" in tags and "max" in tags


def test_session_manager_reasoning_effort():
    mock_client = MagicMock()
    mock_settings = MagicMock(return_value={"model": "gpt-5.6-sol", "reasoningEffort": "max"})

    mgr = SessionManager(
        project_root=".",
        create_openai_client=mock_client,
        get_resolved_settings=mock_settings,
    )

    # Initial default from settings
    assert mgr.get_reasoning_effort() == "max"

    # Override reasoning effort
    mgr.set_reasoning_effort("low")
    assert mgr.get_reasoning_effort() == "low"

    mgr.set_reasoning_effort("high")
    assert mgr.get_reasoning_effort() == "high"

    # Normalization on set
    mgr.set_reasoning_effort("DISABLED")
    assert mgr.get_reasoning_effort() == "off"


def test_interactive_effort_choices():
    assert len(REASONING_EFFORT_CHOICES) == 5
    tags = [c[0] for c in REASONING_EFFORT_CHOICES]
    assert tags == ["max", "high", "medium", "low", "off"]
