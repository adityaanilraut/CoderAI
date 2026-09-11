"""Tool platform tests: registry validation, permissions, guards, disposal, cleanup contracts."""

from __future__ import annotations

import json
import pathlib

import pytest

from coderai.background import get_job_store, reset_job_store
from coderai.soul.approval import (
    describe_tool_permission_request,
    evaluate_permission_scopes,
    permission_coverage_gaps,
)
from coderai.spill import cleanup_spill_session, save_text
from coderai.state import clear_session_state
from coderai.tools.file.read import handle as read_handle
from coderai.tools.legacy.registry import ToolRegistry, get_tool_registry
from coderai.tools.legacy.schema import (
    assert_supported_json_schema,
    define_tool,
    validate_json_schema_value,
)
from coderai.tools.legacy.terminal import (
    handle_terminal_close_tool,
    handle_terminal_list_tool,
    handle_terminal_open_tool,
)
from coderai.tools.legacy.types import ToolDefinition, ToolExecutionContext, ValidationError


def test_registry_validates_required_arguments():
    """Missing required args raise ValidationError; valid args pass through."""
    registry = ToolRegistry()
    with pytest.raises(ValidationError, match="missing required argument 'file_path'"):
        registry.validate_arguments("read", {})
    assert registry.validate_arguments("read", {"file_path": "foo.py"})["file_path"] == "foo.py"


def test_registry_rejects_wrong_types_and_enums():
    """Type mismatches and bad enums raise ValidationError with a clear reason."""
    registry = ToolRegistry()
    with pytest.raises(ValidationError, match="must be a string"):
        registry.validate_arguments("read", {"file_path": 12345})
    with pytest.raises(ValidationError, match="invalid value"):
        registry.validate_arguments("str_replace_editor", {"path": "f.py", "command": "nope"})


def test_registry_sanitizes_openai_schemas():
    """Exported OpenAI schemas strip unsupported JSON-schema keywords."""
    schemas = ToolRegistry().to_openai_schemas()
    blob = json.dumps(schemas)
    assert '"uniqueItems"' not in blob and '"$schema"' not in blob and '"$id"' not in blob


def test_registry_presets_match_canonical_sets():
    """Core and shell_edit presets expose exactly the documented tool names."""
    registry = ToolRegistry()
    core = {s["function"]["name"] for s in registry.to_openai_schemas(options={"preset": "core"})}
    shell = {
        s["function"]["name"] for s in registry.to_openai_schemas(options={"preset": "shell_edit"})
    }
    assert core == {"bash", "str_replace_editor", "edit", "read", "write", "glob", "grep"}
    assert shell == {"bash", "str_replace_editor"}


def test_registry_permission_covers_all_tools():
    """Every registered tool maps to a known permission scope set."""
    assert permission_coverage_gaps() == set()


def test_registry_unknown_scope_fails_closed_to_ask(tmp_path):
    """Unclassified dynamic tools request unknown scope which evaluates to ask."""
    registry = get_tool_registry()
    unregister = registry.register(ToolDefinition(name="unclassified_dynamic_tool"))
    try:
        req = describe_tool_permission_request(
            session_id="s",
            project_root=str(tmp_path),
            tool_call={
                "id": "d",
                "function": {"name": "unclassified_dynamic_tool", "arguments": "{}"},
            },
        )
        assert req["scopes"] == ["unknown"]
        assert evaluate_permission_scopes(req["scopes"]) == "ask"
        safe = describe_tool_permission_request(
            session_id="s",
            project_root=str(tmp_path),
            tool_call={"id": "j", "function": {"name": "job_list", "arguments": "{}"}},
        )
        assert safe["scopes"] == [] and evaluate_permission_scopes([]) == "allow"
    finally:
        unregister()


def test_registry_guard_blocks_destructive_command():
    """Monotonic guards veto destructive bash while allowing safe commands."""
    registry = ToolRegistry()
    dispose = registry.guard(
        lambda tool_def, args, ctx: (
            "Destructive command rejected"
            if tool_def.name == "bash" and "rm -rf" in str(args.get("command", ""))
            else None
        )
    )
    guard = registry._global_layer.guards[0]
    tool = registry.get("bash")
    assert guard(tool, {"command": "rm -rf /"}, None) == "Destructive command rejected"
    assert guard(tool, {"command": "git status"}, None) is None
    dispose()
    assert registry._global_layer.guards == []


def test_registry_restriction_scopes_subagent_safely():
    """Deny restrictions mask mutating tools per-scope and lift on dispose."""
    registry = ToolRegistry()
    dispose = registry.restrict({"deny": ["write", "edit", "bash"]}, scope="subagent_ro")
    try:
        assert registry.has_tool("read", scope="subagent_ro")
        assert not registry.has_tool("write", scope="subagent_ro")
        assert registry.has_tool("write")
    finally:
        dispose()
    assert registry.has_tool("write", scope="subagent_ro")


def test_registry_unregister_emits_change_events():
    """Register/unregister disposers fire change listeners exactly once each."""
    registry = ToolRegistry()
    events: list[str] = []
    unsub = registry.on_change(lambda: events.append("changed"))
    unreg = registry.register(define_tool(name="temp_probe", description="probe", parameters={}))
    assert registry.has_tool("temp_probe") and events == ["changed"]
    unreg()
    assert not registry.has_tool("temp_probe") and events == ["changed", "changed"]
    unsub()


def test_registry_schema_rejects_unsupported_keywords():
    """Schema DSL validates types, enums, and rejects unsupported declarations."""
    assert_supported_json_schema({"type": "object", "properties": {"p": {"type": "string"}}})
    with pytest.raises(ValueError):
        assert_supported_json_schema({"type": "nope"})
    violations = validate_json_schema_value(
        {
            "type": "object",
            "properties": {"role": {"type": "string", "enum": ["a", "b"]}},
            "required": ["role"],
            "additionalProperties": False,
        },
        {"role": "zzz", "extra": 1},
    )
    assert any("invalid value" in v for v in violations)
    assert any("unexpected property" in v for v in violations)


def test_registry_concurrency_marks_read_safe_write_exclusive():
    """Reads are concurrency-safe while writes and mutations are exclusive."""
    registry = get_tool_registry()
    assert registry.get("read").check_concurrency_safe({}) is True
    assert registry.get("glob").check_concurrency_safe({}) is True
    assert registry.get("write").check_concurrency_safe({}) is False
    assert registry.get("bash").check_concurrency_safe({}) is False
    sre = registry.get("str_replace_editor")
    assert sre.check_concurrency_safe({"command": "view"}) is True
    assert sre.check_concurrency_safe({"command": "str_replace"}) is False


def test_platform_spill_cleanup_removes_session_files(tmp_path):
    """Spill files are private to the session and cleanup deletes them."""
    ref = save_text(
        session_id="spill_sess", suggested_name="../evil.txt", content="hello", root=tmp_path
    )
    loc = pathlib.Path(ref.locator)
    assert loc.is_file() and "/" not in loc.name
    cleanup_spill_session("spill_sess", root=tmp_path)
    assert not loc.exists()


def test_platform_jobs_kill_all_terminates_running():
    """kill_all stops every running job for the session."""
    reset_job_store()
    store = get_job_store()
    store.start(job_id="k1", session_id="ks", kind="bash", label="a")
    store.start(job_id="k2", session_id="ks", kind="bash", label="b")
    assert sorted(store.kill_all("ks", reason="test")) == ["k1", "k2"]
    assert all(j.status == "killed" for j in store.list("ks"))
    reset_job_store()


def test_platform_read_rejects_binary_safely(tmp_path):
    """Binary files return a warning instead of decoded garbage."""
    p = tmp_path / "blob.bin"
    p.write_bytes(bytes([0, 1, 2, 255, 254]) + b"hello")
    res = read_handle(
        {"file_path": str(p)}, {"session_id": "bin_sess", "project_root": str(tmp_path)}
    )
    assert res.ok and res.output == "WARNING: File is binary."
    assert res.metadata["isBinary"] is True


def test_platform_terminal_close_removes_session(tmp_path):
    """Closed PTY sessions disappear from subsequent terminal listings."""
    clear_session_state("plat_term")
    ctx = ToolExecutionContext(session_id="plat_term", project_root=str(tmp_path))
    opened = handle_terminal_open_tool({"type": "sh", "name": "platform_probe"}, ctx)
    assert opened.ok
    sid = opened.metadata["sessionId"]
    assert handle_terminal_close_tool({"sessionId": sid}, ctx).ok
    remaining = handle_terminal_list_tool({}, ctx).metadata["sessions"]
    assert all(s["sessionId"] != sid for s in remaining)
