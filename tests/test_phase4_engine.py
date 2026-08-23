"""Phase 4: Hardening & Parity Verification Unit Tests.

Validates:
1. OS Sandbox presets: read-only, workspace-write, danger-full-access.
2. Sandbox Seatbelt (macOS) and Bubblewrap (Linux) command wrapping.
3. Stream resilience: repetition loop collapse, error formatting.
4. Scope security rails and plan mode constraints.
"""

from coderai.core.sandbox import (
    preset_permissions,
    parse_sandbox_mode,
    build_seatbelt_profile,
    wrap_sandbox_command,
    sandbox_policy_prompt,
)
from coderai.core.session import sanitize_repetition_loops
from coderai.core.common.llm_error import describe_llm_error
from coderai.core.permissions import (
    compute_tool_call_permissions,
    PLAN_MODE_FORCE_ASK_SCOPES,
)


def test_sandbox_preset_parsing_and_scopes():
    assert parse_sandbox_mode("readonly") == "read-only"
    assert parse_sandbox_mode("workspace_write") == "workspace-write"
    assert parse_sandbox_mode("none") == "danger-full-access"

    ro = preset_permissions("read-only")
    assert "write-in-cwd" in ro["deny"]
    assert "read-in-cwd" in ro["allow"]
    assert "network" in ro["ask"]

    ww = preset_permissions("workspace-write")
    assert "write-in-cwd" in ww["allow"]
    assert "write-out-cwd" in ww["deny"]
    assert "mutate-git-log" in ww["ask"]

    dfa = preset_permissions("danger-full-access")
    assert len(dfa["deny"]) == 0
    assert dfa["defaultMode"] == "allowAll"


def test_sandbox_profile_generation_and_wrapping():
    # Test macOS Seatbelt profile generation
    sb_ro = build_seatbelt_profile("read-only", "/tmp/project")
    assert "(version 1)" in sb_ro
    assert "(deny default)" in sb_ro

    sb_ww = build_seatbelt_profile("workspace-write", "/tmp/project")
    assert "/tmp/project" in sb_ww

    # Test policy prompt text
    prompt_ro = sandbox_policy_prompt("read-only")
    assert "read-only" in prompt_ro

    # Test danger-full-access does not alter argv
    argv = ["echo", "hello"]
    wrapped_argv, meta = wrap_sandbox_command(
        argv, mode="danger-full-access", workspace_root="/tmp/project"
    )
    assert wrapped_argv == argv
    assert meta["sandboxApplied"] is False


def test_stream_resilience_repetition_loop_sanitizer():
    normal_text = "This is a standard assistant reply explaining code."
    assert sanitize_repetition_loops(normal_text) == normal_text

    # Degenerate repeating loop
    repeating_unit = "function test() { return 1; }\n"
    degenerate_text = "Here is the code:\n" + (repeating_unit * 30)
    sanitized = sanitize_repetition_loops(degenerate_text)
    assert len(sanitized) < len(degenerate_text)

    # Test error description formatter
    err_desc = describe_llm_error(ValueError("Invalid token limit"))
    assert "Invalid token limit" in err_desc


def test_plan_mode_forced_ask_scopes():
    tool_calls = [
        {
            "id": "tc1",
            "type": "function",
            "function": {"name": "write", "arguments": '{"file_path": "foo.py", "content": "..."}'},
        }
    ]

    # In plan mode, write operations are forced to ask
    perm_plan = compute_tool_call_permissions(
        session_id="s_plan",
        project_root="/tmp/test_project",
        tool_calls=tool_calls,
        settings={"defaultMode": "allowAll"},
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert perm_plan["askPermissions"] is not None
    assert len(perm_plan["askPermissions"]) > 0
