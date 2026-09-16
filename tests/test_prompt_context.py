"""Consolidated prompt-context: usage formats, cache prefixes, effort, streams, errors, refs."""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import MagicMock

from coderai.ui.shell.app import _StreamState
from coderai.ui.shell.prompt import expand_file_mentions
from coderai.utils.path import normalize_line_endings
from coderai.utils.common.invariants import (
    InvariantViolation,
    assert_session_invariants,
    verify_monotonic_sequence_numbers,
    verify_paired_tool_calls,
    verify_session_invariants,
    verify_turn_step_boundaries,
)
from coderai.utils.common.llm_error import describe_llm_error, mask_sensitive
from coderai.utils.common.llm_retry import classify_llm_failure, is_failover_eligible
from coderai.utils.common.message_converter import OpenAIMessageConverter
from coderai.utils.common.model_capabilities import (
    defaults_to_thinking_mode,
    get_default_reasoning_effort,
    get_supported_reasoning_efforts,
    resolve_adaptive_reasoning_effort,
)
from coderai.utils.common.openai_thinking import (
    ANTHROPIC_THINKING_BUDGETS,
    GEMINI_THINKING_BUDGETS,
    build_thinking_request_options,
    get_thinking_token_budget,
    normalize_reasoning_effort,
)
from coderai.utils.common.session_reference import (
    extract_session_reference_ids,
    render_session_snapshot,
    resolve_session_references,
)
from coderai.utils.common.usage import accumulate_usage_dict, extract_usage_dict
from coderai.prompt import (
    CACHE_BOUNDARY_TOKEN,
    build_cache_stabilized_messages,
    format_tool_definitions,
    get_runtime_context,
    get_system_prompt,
    get_tools,
)
from coderai.soul.session.manager import (
    SessionManager,
    SessionMessage,
    _call_stream_or_sync,
    sanitize_repetition_loops,
)
from coderai.soul.session.store import JsonlSessionStore
from coderai.tools.legacy.sanitizer import sanitize_text, sanitize_tool_output
from coderai.tools.legacy.types import ToolResult


def _msg(id: str, role: str, content: str = "", **kw: Any) -> SessionMessage:
    """Build a SessionMessage with test defaults."""
    return SessionMessage(id=id, session_id="s", role=role, content=content, **kw)


class _StreamChunk:
    """Fake streaming chunk with optional content and usage payloads."""

    def __init__(self, content: str | None = None, usage: dict | None = None) -> None:
        delta = type(
            "Delta",
            (),
            {"content": content, "reasoning_content": None, "refusal": None, "tool_calls": None},
        )()
        self.choices = [type("Choice", (), {"delta": delta})()] if content else []
        self.usage = type("Usage", (), usage)() if usage else None


def _stream_client(seen: list, *chunks: _StreamChunk) -> Any:
    """Fake OpenAI client replaying chunks while recording requests."""
    create = staticmethod(lambda **kw: (seen.append(kw), iter(chunks))[1])
    return type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": type("C", (), {"create": create})()})()},
    )()


def test_usage_extract_deepseek_format_reports_cache_hits():
    """DeepSeek hit/miss token fields map onto cached counters."""
    u = extract_usage_dict(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 150,
            "total_tokens": 1350,
            "prompt_cache_hit_tokens": 1024,
            "prompt_cache_miss_tokens": 176,
        }
    )
    assert (u["prompt_tokens"], u["cached_tokens"], u["prompt_cache_hit_tokens"]) == (
        1200,
        1024,
        1024,
    )
    assert u["prompt_cache_miss_tokens"] == 176


def test_usage_extract_openai_compatible_format_reads_cached_details():
    """OpenAI prompt_tokens_details.cached_tokens derives hit and miss counts."""
    for prompt, cached in ((2000, 1500), (3000, 2500)):
        u = extract_usage_dict(
            {
                "prompt_tokens": prompt,
                "completion_tokens": 100,
                "total_tokens": prompt + 100,
                "prompt_tokens_details": {"cached_tokens": cached},
            }
        )
        assert u["cached_tokens"] == u["prompt_cache_hit_tokens"] == cached
        assert u["prompt_cache_miss_tokens"] == prompt - cached


def test_usage_extract_anthropic_format_maps_cache_tokens():
    """Anthropic input/output plus cache_read tokens map onto unified counters."""
    u = extract_usage_dict(
        {
            "input_tokens": 5000,
            "output_tokens": 450,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 1000,
        }
    )
    assert (u["prompt_tokens"], u["completion_tokens"], u["total_tokens"]) == (5000, 450, 5450)
    assert (u["cached_tokens"], u["uncached_tokens"]) == (4000, 1000)


def test_usage_extract_object_attributes_supports_sdk_objects():
    """SDK-style usage objects expose the same fields as plain dicts."""
    details = type("Details", (), {"cached_tokens": 640})()
    raw = type(
        "Usage",
        (),
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 640,
            "prompt_cache_miss_tokens": 360,
            "prompt_tokens_details": details,
        },
    )()
    u = extract_usage_dict(raw)
    assert (u["prompt_tokens"], u["cached_tokens"], u["prompt_cache_miss_tokens"]) == (
        1000,
        640,
        360,
    )


def test_usage_accumulate_sums_cached_and_miss_tokens():
    """Accumulation preserves cached and miss counts across turns."""
    acc = accumulate_usage_dict(
        None,
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1000,
        },
    )
    acc = accumulate_usage_dict(
        acc,
        {
            "prompt_tokens": 1200,
            "completion_tokens": 150,
            "total_tokens": 1350,
            "prompt_cache_hit_tokens": 1000,
            "prompt_cache_miss_tokens": 200,
        },
    )
    assert (acc["prompt_tokens"], acc["completion_tokens"], acc["cached_tokens"]) == (
        2200,
        250,
        1000,
    )
    assert acc["prompt_cache_miss_tokens"] == 1200


def test_prompt_cache_prefix_reuses_cached_blocks(tmp_path: pathlib.Path):
    """Appended turns preserve a bitwise-identical message prefix for KV-cache reuse."""
    conv = OpenAIMessageConverter()
    base = [
        _msg("m1", "system", get_system_prompt({"workspaceRoot": str(tmp_path)})),
        _msg("m2", "user", "Refactor this function"),
    ]
    first = conv.convert_session_messages(base, "deepseek-v4-pro", thinking_enabled=True)
    grown = base + [
        _msg(
            "m3",
            "assistant",
            "",
            thinking="Inspect first.",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"file_path": "a.py"}'},
                }
            ],
        ),
        _msg("m4", "tool", "def foo(): pass", tool_call_id="c1"),
    ]
    second = conv.convert_session_messages(grown, "deepseek-v4-pro", thinking_enabled=True)
    assert len(second) > len(first)
    assert second[: len(first)] == first


def test_prompt_cache_anthropic_breakpoints_mark_system_and_penultimate(tmp_path: pathlib.Path):
    """Claude payloads carry ephemeral cache_control on system and penultimate user turns."""
    conv = OpenAIMessageConverter()
    msgs = [
        _msg("s1", "system", get_system_prompt({"workspaceRoot": str(tmp_path)})),
        _msg("u1", "user", "Turn 1 goal"),
        _msg("a1", "assistant", "Step 1"),
        _msg("u2", "user", "Turn 2 goal"),
        _msg("a2", "assistant", "Step 2"),
        _msg("u3", "user", "Turn 3 goal"),
    ]
    converted = conv.convert_session_messages(msgs, "claude-3-7-sonnet")
    assert converted[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert converted[3]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_tool_breakpoints():
    """Tool payloads attach cache_control to the final tool definition for Claude."""
    from coderai.utils.common.message_converter import apply_tool_cache_control

    tools = [
        {"type": "function", "function": {"name": "read"}},
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "write"}},
    ]
    cached_tools = apply_tool_cache_control(tools, "claude-3-7-sonnet")
    assert cached_tools is not None
    assert "cache_control" not in cached_tools[0]
    assert cached_tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_multi_step_tool_loop_breakpoint(tmp_path: pathlib.Path):
    """Multi-step tool execution loops mark the latest tool result with cache_control."""
    conv = OpenAIMessageConverter()
    msgs = [
        _msg("s1", "system", get_system_prompt({"workspaceRoot": str(tmp_path)})),
        _msg("u1", "user", "Perform multi-step task"),
        _msg("a1", "assistant", "", tool_calls=[{"id": "tc1", "function": {"name": "bash", "arguments": "{}"}}]),
        _msg("t1", "tool", "output of step 1", tool_call_id="tc1"),
        _msg("a2", "assistant", "", tool_calls=[{"id": "tc2", "function": {"name": "read", "arguments": "{}"}}]),
        _msg("t2", "tool", "output of step 2", tool_call_id="tc2"),
    ]
    converted = conv.convert_session_messages(msgs, "claude-3-7-sonnet")
    # System prompt marked
    assert converted[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Initial user prompt marked
    assert converted[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Latest completed tool result marked to cache entire execution prefix up to step 2
    assert converted[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_stabilized_messages_insert_boundary_token():
    """Stabilized payloads prefix the boundary token and order tools deterministically."""
    stabilized, tools = build_cache_stabilized_messages(
        [{"role": "user", "content": "First turn"}, {"role": "assistant", "content": "Answer"}],
        system_prompt="You are a helpful coding assistant.",
        tools=[
            {"type": "function", "function": {"name": "write", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}},
        ],
    )
    assert stabilized[0]["role"] == "system" and CACHE_BOUNDARY_TOKEN in stabilized[0]["content"]
    assert stabilized[0]["cache_control"] == {"type": "ephemeral"}
    assert [t["function"]["name"] for t in tools or []] == ["bash", "write"]


def test_prompt_system_static_byte_identity_across_workspaces(tmp_path: pathlib.Path):
    """System prompt bytes are identical across workspaces to maximize prefix cache hits."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    prompt_a = get_system_prompt({"workspaceRoot": str(tmp_path / "a"), "nonInteractive": True})
    prompt_b = get_system_prompt({"workspaceRoot": str(tmp_path / "b"), "nonInteractive": True})
    assert prompt_a == prompt_b and "Mental Post-Fix Trace" in prompt_a


def test_prompt_runtime_context_static_prefix_suppresses_dates():
    """Static runtime context omits volatile dates while keeping model and root."""
    dynamic = get_runtime_context(
        "/test/root", model="deepseek-v4-flash", suppress_dynamic_time=False
    )
    static = get_runtime_context(
        "/test/root", model="deepseek-v4-flash", suppress_dynamic_time=True
    )
    assert "Today is" in dynamic and "Today is" not in static
    assert "Current LLM model: deepseek-v4-flash." in static


def test_prompt_tool_ordering_is_deterministic_across_permutations():
    """Tool definitions serialize identically regardless of input permutation."""
    tools = [
        {"type": "function", "function": {"name": n, "description": n}}
        for n in ("read", "bash", "edit", "grep", "glob")
    ]
    res1 = format_tool_definitions([tools[4], tools[0], tools[2], tools[1], tools[3]])
    res2 = format_tool_definitions([tools[1], tools[3], tools[0], tools[4], tools[2]])
    assert json.dumps(res1, sort_keys=True) == json.dumps(res2, sort_keys=True)


def test_prompt_tool_schema_canonical_sorting_orders_properties():
    """Core tool parameter properties serialize in alphabetical order."""
    for tool in get_tools({"preset": "core"}):
        props = tool["function"]["parameters"].get("properties", {})
        assert list(props) == sorted(props)


def test_prompt_adaptive_effort_scales_down_on_later_turns():
    """Adaptive effort starts high on turn one and drops on iterative steps."""
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=1, step=1) == "high"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-flash", turn=2, step=1) == "low"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-pro", turn=1, step=1) == "max"
    assert resolve_adaptive_reasoning_effort("deepseek-v4-pro", turn=2, step=1) == "high"
    assert (
        resolve_adaptive_reasoning_effort(
            "deepseek-v4-flash", turn=2, step=1, explicit_effort="max"
        )
        == "max"
    )


def test_reasoning_effort_normalizes_legacy_values():
    """Legacy aliases, casing, and unknown values normalize to canonical levels."""
    assert normalize_reasoning_effort("none") == normalize_reasoning_effort("disabled") == "off"
    assert normalize_reasoning_effort("xhigh") == "max"
    assert (
        normalize_reasoning_effort("  HIGH  ") == "high"
        and normalize_reasoning_effort("LoW") == "low"
    )
    assert normalize_reasoning_effort("") == normalize_reasoning_effort(None) == "max"
    assert normalize_reasoning_effort("super-ultra-high") == "max"


def test_reasoning_effort_wire_opts_openai_maps_max_to_high():
    """OpenAI wire passes reasoning_effort directly and maps max/disabled to API values."""
    assert build_thinking_request_options(True, model="gpt-5.6-sol", reasoning_effort="high") == {
        "reasoning_effort": "high"
    }
    assert build_thinking_request_options(True, model="o3", reasoning_effort="max") == {
        "reasoning_effort": "high"
    }
    assert build_thinking_request_options(False, model="gpt-5.6-sol", has_tools=True) == {
        "reasoning_effort": "none"
    }
    assert build_thinking_request_options(
        True, model="gpt-5.6-sol", reasoning_effort="off", has_tools=True
    ) == {"reasoning_effort": "none"}


def test_reasoning_effort_wire_opts_deepseek_gemini_use_extra_body():
    """DeepSeek and Gemini carry effort in extra_body and send nothing when off."""
    assert build_thinking_request_options(
        True, model="deepseek-v4-pro", reasoning_effort="max"
    ) == {"extra_body": {"reasoning_effort": "max"}}
    assert build_thinking_request_options(
        True, model="gemini-3.7-flash", reasoning_effort="low"
    ) == {"extra_body": {"reasoning_effort": "low"}}
    assert (
        build_thinking_request_options(True, model="deepseek-v4-pro", reasoning_effort="off") == {}
    )
    assert (
        build_thinking_request_options(True, model="gemini-3.7-flash", reasoning_effort="off") == {}
    )


def test_reasoning_effort_thinking_budgets_match_provider_tables():
    """Provider thinking budgets resolve per effort level and zero out when off."""
    assert get_thinking_token_budget("low", provider="gemini") == GEMINI_THINKING_BUDGETS["low"]
    assert get_thinking_token_budget("max", provider="gemini") == GEMINI_THINKING_BUDGETS["max"]
    assert (
        get_thinking_token_budget("high", provider="anthropic")
        == ANTHROPIC_THINKING_BUDGETS["high"]
    )
    assert (
        get_thinking_token_budget("off", provider="gemini")
        == get_thinking_token_budget("off", provider="anthropic")
        == 0
    )


def test_reasoning_effort_model_capabilities_report_supported_levels():
    """Capability helpers report supported efforts and defaults per model."""
    assert get_supported_reasoning_efforts("gpt-5.6-sol") == ["off", "low", "medium", "high", "max"]
    assert get_supported_reasoning_efforts("unknown-text-model") == ["off"]
    assert get_default_reasoning_effort("deepseek-v4-pro") == "max"
    assert get_default_reasoning_effort("gemini-3.7-flash") == "low"
    assert defaults_to_thinking_mode("claude-3-7-sonnet") is True


def test_deepseek_flash_v41_capabilities():
    """deepseek-flash (V4.1) is thinking-default, fast, multimodal with full efforts."""
    from coderai.utils.common.model_capabilities import (
        DEEPSEEK_MODELS,
        is_fast_model,
        supports_multimodal,
    )

    assert "deepseek-flash" in DEEPSEEK_MODELS
    assert defaults_to_thinking_mode("deepseek-flash") is True
    assert is_fast_model("deepseek-flash") is True
    assert supports_multimodal("deepseek-flash") is True
    assert not supports_multimodal("deepseek-flash", mode="off")
    assert get_supported_reasoning_efforts("deepseek-flash") == [
        "off",
        "low",
        "medium",
        "high",
        "max",
    ]
    # Legacy v4-flash alias is served by the same V4.1 backend.
    assert defaults_to_thinking_mode("deepseek-v4-flash") is True
    assert supports_multimodal("deepseek-v4-flash") is True
    # Pro remains the non-multimodal reasoning flagship.
    assert not supports_multimodal("deepseek-v4-pro")


def test_deepseek_provider_lists_current_models():
    """DeepSeek provider registry drops retired chat/reasoner aliases."""
    from coderai.config import KNOWN_PROVIDERS

    info = KNOWN_PROVIDERS["deepseek"]
    assert info["default_model"] == "deepseek-flash"
    assert "deepseek-flash" in info["models"]
    assert "deepseek-v4-pro" in info["models"]
    assert "deepseek-chat" not in info["models"]
    assert "deepseek-reasoner" not in info["models"]


def test_deepseek_context_window_is_1m():
    """Current DeepSeek models resolve to the 1M-token context window."""
    from coderai.prompt import get_model_context_limit

    assert get_model_context_limit("deepseek-flash") == 1_000_000
    assert get_model_context_limit("deepseek-v4-pro") == 1_000_000
    assert get_model_context_limit("deepseek-v4-flash") == 1_000_000


def test_reasoning_effort_session_manager_normalizes_on_set():
    """SessionManager reads default effort from settings and normalizes overrides."""
    mgr = SessionManager(
        project_root=".",
        create_openai_client=MagicMock(),
        get_resolved_settings=MagicMock(
            return_value={"model": "gpt-5.6-sol", "reasoningEffort": "max"}
        ),
    )
    assert mgr.get_reasoning_effort() == "max"
    mgr.set_reasoning_effort("low")
    assert mgr.get_reasoning_effort() == "low"
    mgr.set_reasoning_effort("DISABLED")
    assert mgr.get_reasoning_effort() == "off"


def test_stream_repetition_sanitizer_collapses_loops_only():
    """Degenerate repeated phrases collapse while normal markdown passes through."""
    sanitized = sanitize_repetition_loops("- Parses the main te" * 6 + "- Done.")
    assert "[truncated repetition loop]" in sanitized and "- Done." in sanitized
    normal = "## Summary\n- Step 1: Install\n- Step 2: Test\n"
    assert sanitize_repetition_loops(normal) == normal


def test_stream_state_newline_flushes_buffered_content(capsys):
    """ensure_newline emits exactly one newline after streamed chunks."""
    state = _StreamState()
    assert state.had_streamed() is False and state.ensure_newline() is False
    state.on_chunk("Generating text...")
    assert state.ensure_newline() is True and state.had_streamed() is False
    assert capsys.readouterr().out == "Generating text...\n"


def test_stream_state_thinking_finalizes_once():
    """Thinking finalizes on first content chunk or newline, then resets cleanly."""
    state = _StreamState()
    state.on_thinking_chunk("Analyzing...")
    assert state.thinking_streamer.is_active is True
    state.on_chunk("Plan ready.")
    assert state.thinking_streamer.is_active is False and state.thinking_rendered is True
    state.reset()
    assert state.thinking_rendered is False
    thinking_only = _StreamState()
    thinking_only.on_thinking_chunk("Reasoning...")
    thinking_only.ensure_newline()
    assert thinking_only.thinking_rendered is True


def test_stream_call_passes_usage_options_and_collects_tokens():
    """Streaming requests ask for usage and merge chunk content plus totals."""
    seen: list[dict[str, Any]] = []
    res = _call_stream_or_sync(
        _stream_client(
            seen,
            _StreamChunk("Hello "),
            _StreamChunk("world!"),
            _StreamChunk(usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}),
        ),
        {"model": "gpt-4o", "messages": []},
    )
    assert seen[0].get("stream") is True and seen[0].get("stream_options") == {
        "include_usage": True
    }
    assert res["choices"][0]["message"]["content"] == "Hello world!"
    assert res["usage"]["total_tokens"] == 17


def test_stream_call_fallback_estimates_tokens_without_usage():
    """Streams without usage metadata still yield a positive token estimate."""
    res = _call_stream_or_sync(
        _stream_client([], _StreamChunk("A" * 40)), {"model": "gpt-4o", "messages": []}
    )
    assert res["choices"][0]["message"]["content"] == "A" * 40
    assert res["usage"]["total_tokens"] > 0


def test_error_mask_sensitive_redacts_credentials():
    """API keys, bearer tokens, query secrets, and JSON secret fields are masked."""
    assert mask_sensitive("Error using key sk-12345678abcdefghij") == "Error using key ***MASKED***"
    assert (
        mask_sensitive("with Authorization: Bearer secret_jwt_token_12345 end")
        == "with Authorization: Bearer ***MASKED*** end"
    )
    assert (
        mask_sensitive("https://api.openai.com/v1/chat?api_key=my_super_secret_key&other=1")
        == "https://api.openai.com/v1/chat?api_key=***MASKED***&other=1"
    )
    assert (
        mask_sensitive('{"api_key": "top_secret_value", "data": 123}')
        == '{"api_key": "***MASKED***", "data": 123}'
    )


def test_error_describe_formats_status_codes_and_ids():
    """HTTP failures render status, code, type, param, and request/trace IDs."""
    desc = describe_llm_error(
        {
            "status": 500,
            "name": "InternalServerError",
            "message": "Server had an error.",
            "code": "internal_error",
            "type": "server_error",
            "param": "messages",
            "headers": {"x-request-id": "req_abc123", "x-ds-trace-id": "trace_xyz789"},
        }
    )
    assert "HTTP 500: Server had an error." in desc and "code: internal_error" in desc
    assert "request ID: req_abc123" in desc and "trace ID: trace_xyz789" in desc


def test_error_describe_unwraps_nested_causes_and_masks_secrets():
    """Chained connection errors surface the root cause with secrets masked."""
    inner, mid, outer = (
        Exception("getaddrinfo ENOTFOUND api.openai.com"),
        Exception("fetch failed"),
        Exception("Connection error"),
    )
    mid.__cause__, outer.__cause__ = inner, mid
    assert "Connection error: getaddrinfo ENOTFOUND api.openai.com" in describe_llm_error(outer)
    leaked = describe_llm_error(Exception("Failed connecting with key sk-abcdef1234567890"))
    assert "sk-abcdef1234567890" not in leaked and "***MASKED***" in leaked


def test_session_refs_extract_ids_from_prompt():
    """@session: and session: tokens extract in order; plain text yields none."""
    assert extract_session_reference_ids("Review @session:ses_abc12345 please.") == ["ses_abc12345"]
    assert extract_session_reference_ids(
        "Compare @session:ses_11111111 with session:ses_22222222"
    ) == ["ses_11111111", "ses_22222222"]
    assert extract_session_reference_ids("No mentions here") == []


def test_session_refs_snapshot_resolves_historical_context(tmp_path: pathlib.Path):
    """Historical sessions render snapshots that resolve into prompt context blocks."""
    sid = "ses_historical_01"
    events = [
        {
            "seq": 1,
            "type": "session/created",
            "sessionId": sid,
            "timestamp": "2026-08-27T00:00:00Z",
        },
        {"seq": 2, "role": "user", "content": "Implement user authentication with JWT tokens"},
        {"seq": 3, "role": "assistant", "content": "Implemented JWT auth with refresh rotation."},
    ]
    path = JsonlSessionStore(str(tmp_path)).messages_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    snapshot = render_session_snapshot(sid, str(tmp_path))
    assert snapshot is not None and sid in snapshot and "JWT" in snapshot
    clean, refs, ctx_block = resolve_session_references(
        str(tmp_path), f"Build on @session:{sid} for OAuth2."
    )
    assert refs[0]["sessionId"] == sid and refs[0]["resolved"] is True
    assert "Referenced Historical Session" in ctx_block and "OAuth2" in clean
    expanded, attached = expand_file_mentions(f"Build on @session:{sid}.", str(tmp_path))
    assert f"session:{sid}" in attached and "Referenced Prior Sessions" in expanded


def test_session_invariants_accept_valid_reject_broken():
    """Valid event streams pass; gaps, dangling calls, and bad boundaries fail."""
    valid = [
        {"seq": 1, "type": "turn/start"},
        {"seq": 2, "type": "step/start"},
        {"seq": 3, "role": "user", "content": "hello"},
        {
            "seq": 4,
            "role": "assistant",
            "tool_calls": [{"id": "t1", "function": {"name": "read"}}],
            "content": "",
        },
        {"seq": 5, "role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"seq": 6, "role": "assistant", "content": "Hi!"},
        {"seq": 7, "type": "step/end"},
        {"seq": 8, "type": "turn/end"},
    ]
    assert verify_session_invariants(valid) == []
    assert_session_invariants(valid)
    assert any(
        "non-monotonic" in v
        for v in verify_monotonic_sequence_numbers([{"seq": 1}, {"seq": 3}, {"seq": 2}])
    )
    assert any(
        "Nested turn/start" in v
        for v in verify_turn_step_boundaries([{"type": "turn/start"}, {"type": "turn/start"}])
    )
    try:
        assert_session_invariants([{"seq": 1}, {"seq": 3}, {"seq": 2}])
        raise AssertionError("expected InvariantViolation")
    except InvariantViolation as exc:
        assert "non-monotonic" in str(exc)


def test_session_paired_tool_calls_flag_orphans():
    """Paired assistant/tool messages pass; orphan calls name the missing id."""
    assert (
        verify_paired_tool_calls(
            [
                {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            ]
        )
        == []
    )
    violations = verify_paired_tool_calls(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_orphan", "function": {"name": "bash"}}],
            },
            {"role": "user", "content": "Next turn"},
        ]
    )
    assert len(violations) > 0 and "call_orphan" in violations[0]


def test_session_line_endings_normalize_to_lf():
    """CRLF and CR line endings normalize to LF."""
    assert normalize_line_endings("line1\r\nline2\rline3\n") == "line1\nline2\nline3\n"


def test_converter_filters_lifecycle_event_roles():
    """Only system/user/assistant/tool roles survive conversion to wire messages."""
    converted = OpenAIMessageConverter().convert_session_messages(
        [
            _msg("s1", "system", "System"),
            _msg("u1", "user", "Goal"),
            _msg("ev1", "turn/start", ""),
            _msg("ev2", "step/start", ""),
            _msg(
                "a1",
                "assistant",
                "Looking",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "glob", "arguments": "{}"},
                    }
                ],
            ),
            _msg("t1", "tool", '["a.py"]', tool_call_id="call_1"),
            _msg("ev3", "step/end", ""),
        ],
        "gpt-5.6-luna",
    )
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "tool"]


def test_converter_omits_empty_reasoning_content():
    """Empty or missing thinking never emits reasoning_content on the wire."""
    converted = OpenAIMessageConverter().convert_session_messages(
        [
            _msg("m1", "user", "hello"),
            _msg("m2", "assistant", "hi", thinking=None),
            _msg("m3", "assistant", "done", thinking=""),
        ],
        "deepseek-v4-pro",
        thinking_enabled=True,
    )
    assert "reasoning_content" not in converted[1] and "reasoning_content" not in converted[2]


def test_converter_recovers_interrupted_tool_pairing():
    """Assistant calls without a tool response gain a synthetic interrupted result."""
    converted = OpenAIMessageConverter().convert_session_messages(
        [
            _msg("1", "system", "System"),
            _msg("2", "user", "Prompt"),
            _msg(
                "3",
                "assistant",
                "",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "edit", "arguments": "{}"},
                    },
                ],
            ),
            _msg("4", "tool", '{"ok": true}', tool_call_id="call_1"),
            _msg("5", "user", "Follow up"),
        ],
        "gpt-4o",
    )
    parsed = json.loads(next(m for m in converted if m.get("tool_call_id") == "call_2")["content"])
    assert parsed["ok"] is False and parsed["metadata"]["interrupted"] is True


def test_converter_prunes_oversized_tool_results():
    """Empty tool output gets a placeholder; oversized output truncates with a marker."""
    conv = OpenAIMessageConverter()
    empty = conv._convert_message(
        _msg("e", "tool", "", tool_call_id="c1"), thinking_enabled=True, model="deepseek-v4-flash"
    )
    assert empty["content"] == "(no output)"
    large = conv._convert_message(
        _msg("l", "tool", "A" * 20_000, tool_call_id="c1"),
        thinking_enabled=True,
        model="deepseek-v4-flash",
        max_tool_result_chars=1000,
    )
    assert len(large["content"]) < 20_000 and "characters omitted" in large["content"]


def test_sanitizer_scrubs_secrets_from_text_and_tool_output():
    """API keys and DB passwords scrub to redacted markers in text and tool results."""
    text, _ = sanitize_text("My openai key is sk-1234567890abcdef1234567890abcdef")
    assert "sk-" not in text and "[REDACTED_OPENAI_KEY]" in text
    scrubbed = sanitize_tool_output(
        ToolResult(
            ok=True,
            name="read",
            output="Config: sk-ant-api03-abcdef1234567890abcdef1234567890",
            error="Error connecting to redis://default:secretpass@127.0.0.1:6379",
        )
    )
    assert (
        "[REDACTED_ANTHROPIC_KEY]" in scrubbed.output and "[REDACTED_DB_PASSWORD]" in scrubbed.error
    )


def test_failover_classifies_retryable_errors_and_eligibility():
    """Context, quota, rate-limit, and server failures are failover-eligible; bad input is not."""
    for msg, kind in (
        ("context_length_exceeded: max 128000 tokens", "CONTEXT_OVERFLOW"),
        ("insufficient_quota: exceeded current quota", "QUOTA_EXCEEDED"),
        ("HTTP 429 Too Many Requests", "RATE_LIMIT"),
        ("HTTP 503 Service Unavailable", "SERVER"),
    ):
        err = Exception(f"Error: {msg}")
        assert classify_llm_failure(err) == kind and is_failover_eligible(err) is True
    invalid = ValueError("Invalid prompt format")
    assert classify_llm_failure(invalid) is None and is_failover_eligible(invalid) is False
