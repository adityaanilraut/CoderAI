"""Tests for Lifecycle Hooks Framework (PreToolUse, PostToolUse, PreStep, PostStep, StopCriteria, matchers, merge precedence)."""

from __future__ import annotations

import json
import pytest
from coderai.core.hooks import (
    HookPoint,
    HookOutput,
    MergedHookOutcome,
    matches_hook_pattern,
    merge_hook_outputs,
    load_hook_config,
    run_hook_point,
    run_pre_tool_use,
)


def test_matches_hook_pattern():
    # Wildcard
    assert matches_hook_pattern("*", "bash") is True
    assert matches_hook_pattern("any", "read") is True
    assert matches_hook_pattern("", "edit") is True

    # Exact and comma separated
    assert matches_hook_pattern("bash, edit, write", "bash") is True
    assert matches_hook_pattern("bash, edit, write", "edit") is True
    assert matches_hook_pattern("bash, edit, write", "read") is False

    # Glob
    assert matches_hook_pattern("web_*", "web_search") is True
    assert matches_hook_pattern("web_*", "web_fetch") is True
    assert matches_hook_pattern("web_*", "bash") is False

    # Regex
    assert matches_hook_pattern(r"^tool_\d+$", "tool_42") is True
    assert matches_hook_pattern(r"^tool_\d+$", "tool_abc") is False


def test_merge_hook_outputs_precedence():
    # Precedence: deny > ask > allow > none
    out1 = HookOutput(decision="allow", additional_context=["Context A"])
    out2 = HookOutput(decision="ask", reason="Needs confirmation")
    out3 = HookOutput(decision="deny", reason="Security policy violation")

    # deny wins over ask and allow
    merged = merge_hook_outputs([out1, out2, out3])
    assert merged.decision == "deny"
    assert "Security policy violation" in (merged.reason or "")
    assert "Context A" in merged.additional_context

    # ask wins over allow
    merged_ask = merge_hook_outputs([out1, out2])
    assert merged_ask.decision == "ask"
    assert "Needs confirmation" in (merged_ask.reason or "")

    # allow wins over none
    out_none = HookOutput(decision="none")
    merged_allow = merge_hook_outputs([out_none, out1])
    assert merged_allow.decision == "allow"


def test_merge_hook_outputs_stop_flag():
    out1 = HookOutput(decision="allow", continue_run=True)
    out2 = HookOutput(decision="allow", continue_run=False, stop_reason="Reached target milestone")

    merged = merge_hook_outputs([out1, out2])
    assert merged.stop is True
    assert merged.stop_reason == "Reached target milestone"


def test_load_hook_config_from_settings(tmp_path):
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "bash", "hooks": [{"command": "echo '{\"decision\":\"allow\"}'"}]}
            ]
        }
    }
    config = load_hook_config(str(tmp_path), settings=settings)
    assert "PreToolUse" in config
    assert len(config["PreToolUse"]) == 1


def test_run_hook_point_execution(tmp_path):
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "bash",
                    "hooks": [
                        {"command": 'python3 -c "import sys, json; print(json.dumps({\'decision\': \'allow\', \'additionalContext\': [\'Safe command\']}))"'}
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "bash",
                    "hooks": [
                        {"command": 'python3 -c "import sys, json; print(json.dumps({\'systemMessages\': [\'Tool finished\']}))"'}
                    ],
                }
            ],
        }
    }

    # PreToolUse
    res_pre = run_hook_point(
        HookPoint.PRE_TOOL_USE,
        payload={"tool_name": "bash", "tool_input": {"command": "ls"}},
        project_root=str(tmp_path),
        settings=settings,
    )
    assert res_pre.decision == "allow"
    assert "Safe command" in res_pre.additional_context

    # Backward compatibility helper
    context = type("Ctx", (), {"project_root": str(tmp_path), "session_id": "sess_1"})()
    decision = run_pre_tool_use("bash", {"command": "ls"}, context, settings=settings)
    assert decision == "allow"

    # PostToolUse
    res_post = run_hook_point(
        HookPoint.POST_TOOL_USE,
        payload={"tool_name": "bash", "tool_result": "output"},
        project_root=str(tmp_path),
        settings=settings,
    )
    assert "Tool finished" in res_post.system_messages
