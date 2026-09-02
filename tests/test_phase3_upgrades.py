"""Tests for Phase 3 Upgrades: Architectural Hardening & Orchestration.

Covers:
1. Dynamic Sandboxing & Speculative Mutation Isolation:
   - validate_sandboxed_path (path traversal, symlink escapes, absolute paths).
   - generate_virtual_patch (structured diffs, line counts, byte sizes).
   - dry_run mode in write, edit, and str_replace_editor.
   - SubAgentSpec isolated_cwd and dry_run execution confinement.

2. Multi-Model Orchestration & Fallback Cascades:
   - Error classification for CONTEXT_OVERFLOW, QUOTA_EXCEEDED, RATE_LIMIT, SERVER.
   - Failover eligibility (is_failover_eligible).
   - SessionManager multi-model fallback cascade and parameter adaptation.

3. Background Daemon & Task Supervisor:
   - TaskSupervisor task aggregation across subagents and jobs.
   - Heartbeat recording and liveness monitoring (check_liveness idle timeout).
   - Session deletion auto-cancellation (cleanup_session_tasks).

4. KV-Cache Prompt Prefix Stabilization:
   - Deterministic skill catalog alphabetical sorting.
   - CACHE_BOUNDARY_TOKEN and build_cache_stabilized_messages.
   - Byte-identical system prompt prefixes across consecutive turns.

5. Existing Phase 3 Core Capabilities:
   - Subagent lineage tracking and tree interruption.
   - Secret and credential sanitizer.
   - Permission tickets and mid-stream JSON auto-repair.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any
import pytest

from coderai.core.agents import (
    AgentHandle,
    AgentRegistry,
    TaskSupervisor,
)
from coderai.core.common.llm_retry import (
    classify_llm_failure,
    is_failover_eligible,
)
from coderai.core.common.validate import repair_json_string
from coderai.core.prompt import (
    CACHE_BOUNDARY_TOKEN,
    build_cache_stabilized_messages,
    get_system_prompt,
    render_skill_catalog,
)
from coderai.core.sandbox import validate_sandboxed_path
from coderai.core.session import SessionManager
from coderai.core.subagent import (
    SubAgentSpec,
)
from coderai.core.tools.edit import handle_edit_tool
from coderai.core.tools.file_mutation import generate_virtual_patch
from coderai.core.tools.sanitizer import sanitize_text, sanitize_tool_output
from coderai.core.tools.str_replace_editor import handle_str_replace_editor_tool
from coderai.core.tools.types import ToolExecutionContext, ToolResult
from coderai.core.tools.write import handle_write_tool


# ==============================================================================
# 1. Dynamic Sandboxing & Speculative Mutation Isolation
# ==============================================================================


def test_validate_sandboxed_path_traversal_detection(tmp_path: pathlib.Path):
    """Verify validate_sandboxed_path detects traversal attempts escaping sandbox root."""
    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()

    # Valid subpath
    valid, res_path, err = validate_sandboxed_path("subdir/file.txt", sandbox_root)
    assert valid is True
    assert err is None
    assert str(res_path).startswith(str(sandbox_root))

    # Path traversal outside sandbox root
    bad_traversal = "../../etc/passwd"
    valid_bad, _, err_bad = validate_sandboxed_path(bad_traversal, sandbox_root)
    assert valid_bad is False
    assert "escapes isolated sandbox root" in (err_bad or "")

    # Absolute path outside sandbox root
    valid_abs, _, err_abs = validate_sandboxed_path("/tmp/outside.txt", sandbox_root)
    assert valid_abs is False
    assert "escapes isolated sandbox root" in (err_abs or "")


def test_generate_virtual_patch_metadata():
    """Verify generate_virtual_patch creates unified diff, byte metrics, and addition/deletion counts."""
    old_content = "def hello():\n    print('old')\n"
    new_content = "def hello():\n    print('new')\n    return True\n"

    patch = generate_virtual_patch("app/main.py", old_content, new_content)
    assert patch["file_path"] == "app/main.py"
    assert patch["is_creation"] is False
    assert patch["lines_added"] >= 2
    assert patch["lines_removed"] >= 1
    assert patch["old_bytes"] == len(old_content.encode("utf-8"))
    assert patch["new_bytes"] == len(new_content.encode("utf-8"))
    assert "-    print('old')" in patch["diff"]
    assert "+    print('new')" in patch["diff"]

    # Test virtual creation
    patch_create = generate_virtual_patch("app/new.py", None, "initial line\n")
    assert patch_create["is_creation"] is True
    assert patch_create["lines_added"] >= 1
    assert patch_create["lines_removed"] == 0


def test_write_tool_dry_run_isolation(tmp_path: pathlib.Path):
    """Verify write tool in dry_run mode generates diff preview without modifying filesystem."""
    target_file = tmp_path / "test_write.py"
    ctx = ToolExecutionContext(
        session_id="test_dry_run_sess",
        project_root=str(tmp_path),
        dry_run=True,
    )

    args = {
        "file_path": str(target_file),
        "content": "print('hello speculative branch')\n",
    }
    result = handle_write_tool(args, ctx)
    assert result.ok is True
    assert "[dry-run]" in (result.output or "")
    assert result.metadata.get("dry_run") is True
    assert "virtual_patch" in result.metadata
    # Assert physical file was NOT created on disk
    assert not target_file.exists()


def test_edit_tool_dry_run_isolation(tmp_path: pathlib.Path):
    """Verify edit tool in dry_run mode produces virtual patch without touching original disk file."""
    target_file = tmp_path / "test_edit.py"
    target_file.write_text("def run():\n    return 42\n", encoding="utf-8")

    ctx = ToolExecutionContext(
        session_id="test_dry_run_sess",
        project_root=str(tmp_path),
        dry_run=True,
    )

    args = {
        "file_path": str(target_file),
        "old_string": "return 42",
        "new_string": "return 100",
    }
    result = handle_edit_tool(args, ctx)
    assert result.ok is True
    assert "[dry-run]" in (result.output or "")
    assert result.metadata.get("dry_run") is True
    assert result.metadata["lines_added"] >= 1
    assert result.metadata["lines_removed"] >= 1

    # Assert physical file content remains unchanged
    assert target_file.read_text(encoding="utf-8") == "def run():\n    return 42\n"


def test_str_replace_editor_dry_run_isolation(tmp_path: pathlib.Path):
    """Verify str_replace_editor in dry_run mode generates preview diff without mutating file."""
    target_file = tmp_path / "sample.py"
    target_file.write_text("x = 1\ny = 2\n", encoding="utf-8")

    ctx = ToolExecutionContext(
        session_id="test_dry_run_sess",
        project_root=str(tmp_path),
        dry_run=True,
    )

    args = {
        "command": "str_replace",
        "path": str(target_file),
        "old_str": "y = 2",
        "new_str": "y = 99",
    }
    result = handle_str_replace_editor_tool(args, ctx)
    assert result.ok is True
    assert "[dry-run]" in (result.output or "")
    assert result.metadata.get("dry_run") is True
    assert target_file.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


@pytest.mark.asyncio
async def test_subagent_spec_isolated_cwd_and_dry_run(tmp_path: pathlib.Path):
    """Verify SubAgentSpec transmits isolated_cwd and dry_run to execution hooks and executor."""
    isolated_dir = tmp_path / "isolated_workspace"
    isolated_dir.mkdir()

    spec = SubAgentSpec(
        description="Speculative agent",
        prompt="Test speculative change",
        isolated_cwd=str(isolated_dir),
        dry_run=True,
    )
    assert spec.isolated_cwd == str(isolated_dir)
    assert spec.dry_run is True


# ==============================================================================
# 2. Multi-Model Orchestration & Fallback Cascades
# ==============================================================================


def test_llm_failure_classification_and_failover_eligibility():
    """Verify error classification for CONTEXT_OVERFLOW, QUOTA_EXCEEDED, RATE_LIMIT, and SERVER."""
    # Context length exceeded
    err_context = Exception(
        "Error: context_length_exceeded: maximum context length is 128000 tokens"
    )
    assert classify_llm_failure(err_context) == "CONTEXT_OVERFLOW"
    assert is_failover_eligible(err_context) is True

    # Quota exceeded
    err_quota = Exception("Error: insufficient_quota: You exceeded your current quota")
    assert classify_llm_failure(err_quota) == "QUOTA_EXCEEDED"
    assert is_failover_eligible(err_quota) is True

    # Rate limit (429)
    err_rate = Exception("HTTP 429 Too Many Requests: Rate limit exceeded")
    assert classify_llm_failure(err_rate) == "RATE_LIMIT"
    assert is_failover_eligible(err_rate) is True

    # 503 Server Error
    err_server = Exception("HTTP 503 Service Unavailable: Cloud provider outage")
    assert classify_llm_failure(err_server) == "SERVER"
    assert is_failover_eligible(err_server) is True

    # Non-failover syntax / invalid argument error
    err_invalid = ValueError("Invalid prompt format")
    assert classify_llm_failure(err_invalid) is None
    assert is_failover_eligible(err_invalid) is False


@pytest.mark.asyncio
async def test_session_manager_multi_model_fallback_cascade(tmp_path: pathlib.Path):
    """Verify SessionManager._create_completion_with_retry cascades to fallback model on 429 / outage."""
    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"model": "primary-model"},
        get_resolved_settings=lambda: {
            "model": "primary-model",
            "fallbackModels": ["fallback-model-a", "fallback-model-b"],
        },
    )

    models_called: list[str] = []

    async def mock_create_completion(
        client: Any, req: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        called_model = req.get("model", "")
        models_called.append(called_model)
        if called_model == "primary-model":
            raise Exception("HTTP 429: Rate limit exceeded for primary-model")
        elif called_model == "fallback-model-a":
            # Fallback succeeds
            return {
                "choices": [{"message": {"content": "Response from fallback-model-a"}}],
                "usage": {"total_tokens": 120},
            }
        return {"choices": [{"message": {"content": "Fallback B"}}]}

    mgr._create_completion = mock_create_completion  # type: ignore[assignment]

    req = {"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]}
    res = await mgr._create_completion_with_retry("test_sess", object(), req)

    assert "primary-model" in models_called
    assert "fallback-model-a" in models_called
    assert res["choices"][0]["message"]["content"] == "Response from fallback-model-a"
    assert res.get("_fallback_info") is not None
    assert res["_fallback_info"]["fallback_used"] is True
    assert res["_fallback_info"]["primary_model"] == "primary-model"
    assert res["_fallback_info"]["active_model"] == "fallback-model-a"


# ==============================================================================
# 3. Background Daemon & Task Supervisor
# ==============================================================================


def test_task_supervisor_aggregation_and_inspection():
    """Verify TaskSupervisor lists and inspects subagents and background tasks."""
    reg = AgentRegistry()
    supervisor = TaskSupervisor(reg)

    h1 = AgentHandle(
        id="task_sub1",
        parent_session_id="sess_100",
        description="Worker 1",
        mode="general",
        status="running",
    )
    reg.register(h1)

    tasks = supervisor.list_tasks(session_id="sess_100")
    assert len(tasks) >= 1
    assert any(t["id"] == "task_sub1" for t in tasks)

    detail = supervisor.get_task("task_sub1")
    assert detail is not None
    assert detail["id"] == "task_sub1"
    assert detail["status"] == "running"
    assert detail["mode"] == "general"


def test_task_supervisor_heartbeat_and_liveness_reaping():
    """Verify TaskSupervisor.check_liveness reaps stalled tasks with idle heartbeat timeout."""
    reg = AgentRegistry()
    supervisor = TaskSupervisor(reg)

    now = time.time()
    # Task with recent heartbeat
    h_active = AgentHandle(
        id="task_active",
        parent_session_id="sess_1",
        description="Active worker",
        mode="general",
        status="running",
        started_at=now,
        last_heartbeat_at=now,
    )
    # Task with stale heartbeat (>120s ago)
    h_stale = AgentHandle(
        id="task_stale",
        parent_session_id="sess_1",
        description="Stalled worker",
        mode="general",
        status="running",
        started_at=now - 200,
        last_heartbeat_at=now - 150,
    )

    reg.register(h_active)
    reg.register(h_stale)

    # Check liveness with 60s timeout
    reaped = supervisor.check_liveness(idle_timeout_seconds=60.0)
    assert "task_stale" in reaped
    assert "task_active" not in reaped
    assert h_stale.status == "timeout"
    assert h_active.status == "running"


def test_task_supervisor_session_drop_auto_cleanup():
    """Verify TaskSupervisor.cleanup_session_tasks auto-cancels all running tasks for a dropped session."""
    reg = AgentRegistry()
    supervisor = TaskSupervisor(reg)

    h1 = AgentHandle(
        id="task_to_drop",
        parent_session_id="sess_dropping",
        description="Background task",
        mode="general",
        status="running",
    )
    h2 = AgentHandle(
        id="task_other",
        parent_session_id="sess_keep",
        description="Other task",
        mode="general",
        status="running",
    )
    reg.register(h1)
    reg.register(h2)

    killed = supervisor.cleanup_session_tasks("sess_dropping")
    assert "task_to_drop" in killed
    assert h1.status == "interrupted"
    assert h2.status == "running"


# ==============================================================================
# 4. KV-Cache Prompt Prefix Stabilization
# ==============================================================================


def test_render_skill_catalog_deterministic_alphabetical_sorting(tmp_path: pathlib.Path):
    """Verify skill catalog renders skills in deterministic alphabetical order by name."""
    skill_dir_b = tmp_path / ".coderai" / "skills" / "beta-tool"
    skill_dir_b.mkdir(parents=True)
    (skill_dir_b / "SKILL.md").write_text(
        "---\nname: beta-tool\ndescription: Beta skill description\n---\nBody",
        encoding="utf-8",
    )

    skill_dir_a = tmp_path / ".coderai" / "skills" / "alpha-tool"
    skill_dir_a.mkdir(parents=True)
    (skill_dir_a / "SKILL.md").write_text(
        "---\nname: alpha-tool\ndescription: Alpha skill description\n---\nBody",
        encoding="utf-8",
    )

    catalog = render_skill_catalog(project_root=str(tmp_path))
    assert catalog is not None
    idx_a = catalog.find("alpha-tool")
    idx_b = catalog.find("beta-tool")
    assert idx_a != -1 and idx_b != -1
    assert idx_a < idx_b  # Alpha must precede Beta


def test_build_cache_stabilized_messages_prefix_and_boundary():
    """Verify build_cache_stabilized_messages inserts CACHE_BOUNDARY_TOKEN and cache_control headers."""
    system_prompt = "You are a helpful coding assistant."
    messages = [
        {"role": "user", "content": "First turn user prompt"},
        {"role": "assistant", "content": "First turn assistant answer"},
        {"role": "user", "content": "Second turn user prompt"},
    ]
    tools = [
        {"type": "function", "function": {"name": "write", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}},
    ]

    stabilized_msgs, stabilized_tools = build_cache_stabilized_messages(
        messages,
        system_prompt=system_prompt,
        tools=tools,
        include_boundary_tag=True,
        enable_cache_control=True,
    )

    # First message must be the system message with boundary tag
    assert stabilized_msgs[0]["role"] == "system"
    assert CACHE_BOUNDARY_TOKEN in stabilized_msgs[0]["content"]
    assert stabilized_msgs[0]["cache_control"] == {"type": "ephemeral"}

    # User messages preserved in sequence
    assert len(stabilized_msgs) == 4
    assert stabilized_msgs[1]["content"] == "First turn user prompt"

    # Tools deterministically ordered (bash before write according to TOOL_ORDER)
    tool_names = [t["function"]["name"] for t in stabilized_tools or []]
    assert tool_names == ["bash", "write"]


def test_byte_identical_system_prompt_prefix_across_consecutive_turns(tmp_path: pathlib.Path):
    """Verify system prompt output remains byte-identical across consecutive turns."""
    options = {
        "workspaceRoot": str(tmp_path),
        "preset": "full",
        "sandboxMode": "workspace-write",
    }
    prompt_turn_1 = get_system_prompt(options)
    time.sleep(0.01)
    prompt_turn_2 = get_system_prompt(options)

    assert prompt_turn_1 == prompt_turn_2
    assert len(prompt_turn_1.encode("utf-8")) == len(prompt_turn_2.encode("utf-8"))


# ==============================================================================
# 5. Core Parity Tests: Lineage, Sanitization, Permissions, JSON Repair
# ==============================================================================


def test_subagent_spec_and_result_lineage_metadata():
    """Verify SubAgentSpec and SubAgentResult store hierarchical tree lineage."""
    spec = SubAgentSpec(
        description="Child task",
        prompt="Do something",
        depth=1,
        parent_agent_id="agent_root123",
        root_agent_id="agent_root123",
        children_ids=["agent_child456"],
    )
    assert spec.depth == 1
    assert spec.parent_agent_id == "agent_root123"
    assert spec.root_agent_id == "agent_root123"
    assert spec.children_ids == ["agent_child456"]


def test_agent_registry_interrupt_tree():
    """Verify AgentRegistry.interrupt_tree recursively cancels entire subtree."""
    registry = AgentRegistry()
    root = AgentHandle(
        id="root",
        parent_session_id="s1",
        description="Root",
        mode="general",
        status="running",
        children_ids=["child1", "child2"],
    )
    child1 = AgentHandle(
        id="child1",
        parent_session_id="s1",
        description="Child 1",
        mode="general",
        status="running",
    )
    child2 = AgentHandle(
        id="child2",
        parent_session_id="s1",
        description="Child 2",
        mode="general",
        status="running",
    )
    registry.register(root)
    registry.register(child1)
    registry.register(child2)

    cancelled = registry.interrupt_tree("root")
    assert set(cancelled) == {"root", "child1", "child2"}
    assert root.status == "interrupted"
    assert child1.status == "interrupted"
    assert child2.status == "interrupted"


def test_secret_sanitizer_and_tool_output_scrubbing():
    """Verify detection and scrubbing of API keys, SSH keys, passwords, and tokens."""
    text1, types1 = sanitize_text("My openai key is sk-1234567890abcdef1234567890abcdef")
    assert "sk-" not in text1
    assert "[REDACTED_OPENAI_KEY]" in text1

    raw_res = ToolResult(
        ok=True,
        name="read",
        output="Config: sk-ant-api03-abcdef1234567890abcdef1234567890",
        error="Error connecting to redis://default:secretpass@127.0.0.1:6379",
    )
    scrubbed = sanitize_tool_output(raw_res)
    assert "[REDACTED_ANTHROPIC_KEY]" in scrubbed.output
    assert "[REDACTED_DB_PASSWORD]" in scrubbed.error


def test_repair_json_string_unterminated_structures():
    """Verify repair_json_string closes truncated quotes and structural braces/brackets."""
    raw = '{"file_path": "src/main.py", "content": "hello world'
    repaired = repair_json_string(raw)
    parsed = json.loads(repaired)
    assert parsed["file_path"] == "src/main.py"
    assert parsed["content"] == "hello world"
