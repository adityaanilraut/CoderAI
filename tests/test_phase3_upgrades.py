"""Tests for Phase 3 Upgrades: Advanced Orchestration & Intelligence.

Verifies:
1. Subagent hierarchical trees, lineage tracking, lifecycle events, and tree interruption.
2. Deterministic tool result secret sanitizer and credential anonymizer.
3. Structured permission escalation handshake (PermissionTicket, scoped grants, timeout, pattern matching).
4. Resilient streaming with mid-stream JSON tool call auto-repair.
"""

from __future__ import annotations

import json
import time
import pytest

from coderai.core.agents import AgentHandle, AgentRegistry, spawn_background_agent
from coderai.core.common.validate import repair_json_string
from coderai.core.permissions import (
    PermissionTicket,
    PermissionTicketRegistry,
    compute_tool_call_permissions,
)
from coderai.core.subagent import (
    MAX_SUBAGENT_DEPTH,
    SubAgentManager,
    SubAgentResult,
    SubAgentSpec,
)
from coderai.core.tools.executor import ToolExecutor
from coderai.core.tools.sanitizer import sanitize_text, sanitize_tool_output
from coderai.core.tools.types import ToolResult


# ============================================================================
# 1. Subagent Hierarchical Trees & Lifecycle Events Tests (Gap 1.1)
# ============================================================================


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

    res = SubAgentResult(
        task_id=spec.task_id,
        session_id="sub_test",
        status="completed",
        summary="Done",
        depth=1,
        parent_agent_id="agent_root123",
        root_agent_id="agent_root123",
        children_ids=["agent_child456"],
        lifecycle_events=[{"type": "subagent/spawn"}, {"type": "subagent/complete"}],
    )
    d = res.to_dict()
    assert d["depth"] == 1
    assert d["parent_agent_id"] == "agent_root123"
    assert d["root_agent_id"] == "agent_root123"
    assert d["children_ids"] == ["agent_child456"]
    assert len(d["lifecycle_events"]) == 2


@pytest.mark.asyncio
async def test_subagent_max_depth_enforcement_and_lifecycle_events():
    """Verify exceeding max depth generates lifecycle error event and halts recursion."""
    manager = SubAgentManager(
        project_root=".",
        create_openai_client=lambda: {"client": None},
    )
    spec = SubAgentSpec(
        description="Deeply nested agent",
        prompt="Explore",
        depth=MAX_SUBAGENT_DEPTH + 1,
        parent_agent_id="agent_parent",
        root_agent_id="agent_root",
    )
    result = await manager.spawn_subagent(spec)
    assert result.status == "failed"
    assert "RecursionLimitError" in (result.error or "")
    assert result.depth == MAX_SUBAGENT_DEPTH + 1
    assert any(evt["type"] == "subagent/spawn" for evt in result.lifecycle_events)
    assert any(evt["type"] == "subagent/error" for evt in result.lifecycle_events)


def test_agent_registry_tree_and_child_navigation():
    """Verify AgentRegistry get_children and get_tree hierarchical representation."""
    registry = AgentRegistry()

    root = AgentHandle(
        id="agent_root",
        parent_session_id="s1",
        description="Root Coordinator",
        mode="general",
        depth=0,
        children_ids=["agent_c1", "agent_c2"],
    )
    c1 = AgentHandle(
        id="agent_c1",
        parent_session_id="s1",
        description="Researcher",
        mode="read_only",
        parent_agent_id="agent_root",
        root_agent_id="agent_root",
        depth=1,
        children_ids=["agent_gc1"],
    )
    c2 = AgentHandle(
        id="agent_c2",
        parent_session_id="s1",
        description="Architect",
        mode="general",
        parent_agent_id="agent_root",
        root_agent_id="agent_root",
        depth=1,
    )
    gc1 = AgentHandle(
        id="agent_gc1",
        parent_session_id="s1",
        description="Code reviewer",
        mode="read_only",
        parent_agent_id="agent_c1",
        root_agent_id="agent_root",
        depth=2,
    )

    registry.register(root)
    registry.register(c1)
    registry.register(c2)
    registry.register(gc1)

    # Check direct children
    children = registry.get_children("agent_root")
    assert len(children) == 2
    assert {c.id for c in children} == {"agent_c1", "agent_c2"}

    # Check full tree
    tree = registry.get_tree("agent_root")
    assert tree is not None
    assert tree["id"] == "agent_root"
    assert len(tree["children"]) == 2
    c1_node = next(c for c in tree["children"] if c["id"] == "agent_c1")
    assert len(c1_node["children"]) == 1
    assert c1_node["children"][0]["id"] == "agent_gc1"


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
        children_ids=["grandchild1"],
    )
    child2 = AgentHandle(
        id="child2",
        parent_session_id="s1",
        description="Child 2",
        mode="general",
        status="running",
    )
    grandchild1 = AgentHandle(
        id="grandchild1",
        parent_session_id="s1",
        description="Grandchild 1",
        mode="general",
        status="running",
    )

    for h in (root, child1, child2, grandchild1):
        registry.register(h)

    cancelled = registry.interrupt_tree("root")
    assert set(cancelled) == {"root", "child1", "child2", "grandchild1"}
    assert root.status == "interrupted"
    assert child1.status == "interrupted"
    assert child2.status == "interrupted"
    assert grandchild1.status == "interrupted"


@pytest.mark.asyncio
async def test_spawn_background_agent_lineage_link():
    """Verify spawn_background_agent establishes parent-child lineage in registry."""
    manager = SubAgentManager(
        project_root=".",
        create_openai_client=lambda: {"client": None},
    )
    root_spec = SubAgentSpec(description="Root task", prompt="Run")
    root_handle = await spawn_background_agent(manager, root_spec)

    child_spec = SubAgentSpec(description="Child task", prompt="Sub run")
    child_handle = await spawn_background_agent(manager, child_spec, parent_agent_id=root_handle.id)

    assert child_handle.parent_agent_id == root_handle.id
    assert child_handle.root_agent_id == root_handle.id
    assert child_handle.depth == 1
    assert child_handle.id in root_handle.children_ids


# ============================================================================
# 2. Deterministic Tool Result Sanitizer & Credential Anonymizer (Gap 2.2)
# ============================================================================


def test_secret_sanitizer_text_patterns():
    """Verify detection and scrubbing of various API keys, SSH keys, passwords, and tokens."""
    # OpenAI key
    text1, types1 = sanitize_text(
        "My openai key is sk-1234567890abcdef1234567890abcdef and sk-proj-abcdef1234567890abcdef1234567890"
    )
    assert "sk-" not in text1
    assert "[REDACTED_OPENAI_KEY]" in text1
    assert "openai_api_key" in types1

    # Anthropic key
    text2, types2 = sanitize_text("Anthropic: sk-ant-api03-abcdef1234567890abcdef1234567890")
    assert "sk-ant-" not in text2
    assert "[REDACTED_ANTHROPIC_KEY]" in text2
    assert "anthropic_api_key" in types2

    # GitHub token
    text3, types3 = sanitize_text("Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz")
    assert "ghp_" not in text3
    assert "[REDACTED_GITHUB_TOKEN]" in text3
    assert "github_token" in types3

    # AWS Access Key ID
    text4, types4 = sanitize_text("AWS ID: AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in text4
    assert "[REDACTED_AWS_KEY_ID]" in text4
    assert "aws_access_key" in types4

    # Private SSH Key
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
    text5, types5 = sanitize_text(f"Here is my key:\n{pem}")
    assert "MIIEowIBAAKCAQEA0" not in text5
    assert "[REDACTED_PRIVATE_KEY]" in text5
    assert "ssh_private_key" in types5

    # Database URI password
    db_uri = "postgres://admin:SuperSecretP@ssword123@db.example.com:5432/production"
    text6, types6 = sanitize_text(f"Connected to {db_uri}")
    assert "SuperSecretP@ssword123" not in text6
    assert "postgres://admin:[REDACTED_DB_PASSWORD]@db.example.com:5432/production" in text6
    assert "database_uri_password" in types6

    # Bearer Header
    bearer = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakThisSignature"
    text7, types7 = sanitize_text(bearer)
    assert "doNotLeakThisSignature" not in text7


def test_sanitize_tool_output_recursive_and_tool_result():
    """Verify sanitize_tool_output operates recursively on dicts, lists, and ToolResult."""
    data = {
        "ok": True,
        "token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "nested": [
            "sk-1234567890abcdef1234567890abcdef",
            {"uri": "postgres://user:password123@localhost/db"},
        ],
    }
    sanitized = sanitize_tool_output(data)
    assert "[REDACTED_GITHUB_TOKEN]" in sanitized["token"]
    assert "[REDACTED_OPENAI_KEY]" in sanitized["nested"][0]
    assert "[REDACTED_DB_PASSWORD]" in sanitized["nested"][1]["uri"]

    tool_res = ToolResult(
        ok=True,
        name="read",
        output="Config: sk-1234567890abcdef1234567890abcdef",
        error="Error connecting to redis://default:secretpass@127.0.0.1:6379",
    )
    scrubbed_res = sanitize_tool_output(tool_res)
    assert "sk-" not in scrubbed_res.output
    assert "secretpass" not in scrubbed_res.error
    assert "[REDACTED_OPENAI_KEY]" in scrubbed_res.output
    assert "[REDACTED_DB_PASSWORD]" in scrubbed_res.error


@pytest.mark.asyncio
async def test_tool_executor_sanitizes_output_before_completion(tmp_path):
    """Verify ToolExecutor automatically anonymizes sensitive outputs."""
    executor = ToolExecutor(str(tmp_path))
    # Test format_tool_result sanitization
    raw_res = ToolResult(
        ok=True,
        name="test_tool",
        output="Secret token: ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    )
    formatted = executor.format_tool_result(raw_res)
    assert "ghp_" not in formatted
    assert "[REDACTED_GITHUB_TOKEN]" in formatted


# ============================================================================
# 3. Structured Permission Escalation Handshake (Gap 5.2)
# ============================================================================


def test_permission_ticket_validation_and_quota():
    """Verify PermissionTicket timeout, usage count quota, and pattern constraints."""
    ticket = PermissionTicket(
        session_id="session_1",
        tool_name="bash",
        scope="write-out-cwd",
        max_uses=2,
        pattern="npm test*",
    )

    # First valid use
    assert ticket.is_valid(tool_name="bash", scope="write-out-cwd", target="npm test")
    assert ticket.consume(tool_name="bash", scope="write-out-cwd", target="npm test")
    assert ticket.use_count == 1

    # Non-matching pattern
    assert not ticket.is_valid(tool_name="bash", scope="write-out-cwd", target="rm -rf /")

    # Second valid use
    assert ticket.consume(tool_name="bash", scope="write-out-cwd", target="npm test:unit")
    assert ticket.use_count == 2

    # Quota exceeded on third use
    assert not ticket.is_valid(tool_name="bash", scope="write-out-cwd", target="npm test")
    assert not ticket.consume(tool_name="bash", scope="write-out-cwd", target="npm test")


def test_permission_ticket_expiry():
    """Verify PermissionTicket expiration duration."""
    ticket = PermissionTicket(
        session_id="session_1",
        tool_name="*",
        scope="*",
        expires_at=time.time() - 1.0,  # Already expired
    )
    assert not ticket.is_valid()
    assert not ticket.consume()


def test_permission_ticket_registry_and_scope_escalation(tmp_path):
    """Verify PermissionTicketRegistry grants bypass 'ask' in compute_tool_call_permissions."""
    registry = PermissionTicketRegistry()
    session_id = "test_escalation_sess"

    # Default settings require ask for write-out-cwd
    settings = {"allow": [], "deny": [], "ask": ["write-out-cwd"], "defaultMode": "allowAll"}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "write",
                "arguments": json.dumps({"file_path": "/tmp/external.txt", "content": "hi"}),
            },
        }
    ]

    # Without ticket -> ask decision
    plan1 = compute_tool_call_permissions(
        session_id=session_id,
        project_root=str(tmp_path),
        tool_calls=tool_calls,
        settings=settings,
        ticket_registry=registry,
    )
    assert plan1["permissions"][0]["permission"] == "ask"
    assert len(plan1["askPermissions"]) == 1

    # Grant ticket for write-out-cwd
    ticket = registry.request_escalation(
        session_id=session_id,
        tool_name="write",
        scope="write-out-cwd",
        max_uses=1,
    )
    assert ticket is not None
    assert len(registry.list_active_tickets(session_id)) == 1

    # With active ticket -> allow decision
    plan2 = compute_tool_call_permissions(
        session_id=session_id,
        project_root=str(tmp_path),
        tool_calls=tool_calls,
        settings=settings,
        ticket_registry=registry,
    )
    assert plan2["permissions"][0]["permission"] == "allow"
    assert len(plan2["askPermissions"]) == 0

    # Revoke ticket -> reverts to ask
    registry.revoke_ticket(ticket.ticket_id)
    plan3 = compute_tool_call_permissions(
        session_id=session_id,
        project_root=str(tmp_path),
        tool_calls=tool_calls,
        settings=settings,
        ticket_registry=registry,
    )
    assert plan3["permissions"][0]["permission"] == "ask"


# ============================================================================
# 4. Resilient Streaming with Mid-Stream Tool Call Repair (Gap 10.1)
# ============================================================================


def test_repair_json_string_unterminated_quotes_and_braces():
    """Verify repair_json_string closes truncated quotes and structural braces/brackets."""
    # Truncated string inside object
    raw1 = '{"file_path": "src/main.py", "content": "hello world'
    repaired1 = repair_json_string(raw1)
    parsed1 = json.loads(repaired1)
    assert parsed1["file_path"] == "src/main.py"
    assert parsed1["content"] == "hello world"

    # Truncated nested lists and objects
    raw2 = '{"items": [{"id": 1}, {"id": 2, "name": "test'
    repaired2 = repair_json_string(raw2)
    parsed2 = json.loads(repaired2)
    assert parsed2["items"][0]["id"] == 1
    assert parsed2["items"][1]["name"] == "test"

    # Trailing dangling comma
    raw3 = '{"file_path": "test.txt", "lines": 42,'
    repaired3 = repair_json_string(raw3)
    parsed3 = json.loads(repaired3)
    assert parsed3["file_path"] == "test.txt"
    assert parsed3["lines"] == 42


def test_repair_json_string_unescaped_newlines():
    """Verify repair_json_string properly escapes raw newlines in string literals."""
    raw = '{\n  "file_path": "a.txt",\n  "content": "line1\nline2"\n}'
    repaired = repair_json_string(raw)
    parsed = json.loads(repaired)
    assert "line1\nline2" in parsed["content"]


def test_tool_executor_parse_tool_arguments_with_auto_repair(tmp_path):
    """Verify ToolExecutor._parse_tool_arguments succeeds on truncated arguments string."""
    executor = ToolExecutor(str(tmp_path))
    broken_args = '{"file_path": "app.py", "command": "run'
    parsed = executor._parse_tool_arguments(broken_args)
    assert parsed["ok"] is True
    assert parsed["args"]["file_path"] == "app.py"
    assert parsed["args"]["command"] == "run"
