"""Phase 2 ports: ApprovalPort is the confirmation seam; tests inject a fake."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import MagicMock

import pytest

from coderAI.core.agent_loop import ExecutionLoop
from coderAI.core.ports import (
    AgentRuntime,
    Approval,
    ApprovalPort,
    AsyncClosePort,
    DenyByDefaultApprovalPort,
    RuntimeView,
    await_approval,
)
from coderAI.core.tool_executor import ToolExecutor


class FakeApprovalPort:
    """In-memory ApprovalPort used by unit tests."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, dict[str, Any], Optional[str]]] = []

    async def request(
        self,
        tool: str,
        args: dict[str, Any],
        preview: Optional[str] = None,
    ) -> Approval:
        self.calls.append((tool, args, preview))
        return Approval(allowed=self.allowed)


def _gated_agent(*, port: Optional[FakeApprovalPort] = None) -> SimpleNamespace:
    info = SimpleNamespace(status="tool_call", current_tool="write_file", agent_id="a1")
    return SimpleNamespace(
        auto_approve=False,
        approval_port=port,
        confirmation_override=None,
        tracker_info=info,
        _sync_tracker=MagicMock(),
        tracker_update=MagicMock(),
        config=SimpleNamespace(approval_timeout_seconds=300),
        tools=SimpleNamespace(get=MagicMock(return_value=None)),
        session=None,
        _tool_approval_allowlist=SimpleNamespace(allows=MagicMock(return_value=False)),
    )


@pytest.mark.asyncio
async def test_executor_allows_when_fake_port_approves() -> None:
    port = FakeApprovalPort(allowed=True)
    executor = ToolExecutor(_gated_agent(port=port))

    allowed = await executor._confirmation_callback(
        "write_file",
        {"path": "x.py", "content": "y"},
        tool_id="t1",
    )

    assert allowed is True
    assert port.calls[0][0] == "write_file"


@pytest.mark.asyncio
async def test_executor_denies_when_fake_port_denies() -> None:
    port = FakeApprovalPort(allowed=False)
    executor = ToolExecutor(_gated_agent(port=port))

    allowed = await executor._confirmation_callback(
        "delete_file",
        {"path": "x.py"},
        tool_id="t2",
    )

    assert allowed is False
    assert port.calls[0][0] == "delete_file"


@pytest.mark.asyncio
async def test_deny_by_default_port_records_and_denies() -> None:
    seen: list[str] = []
    port = DenyByDefaultApprovalPort(on_denied=seen.append)

    result = await port.request("run_command", {"command": "rm -rf /"})

    assert result.allowed is False
    assert port.denied == ["run_command"]
    assert seen == ["run_command"]


@pytest.mark.asyncio
async def test_workspace_trust_prompt_goes_through_approval_port() -> None:
    port = FakeApprovalPort(allowed=True)
    agent = SimpleNamespace(
        approval_port=port,
        hooks_manager=None,
        config=SimpleNamespace(project_root="."),
        _workspace_trusted=False,
        plan_mode=False,
    )
    loop = ExecutionLoop(agent)
    assert await loop._prompt_workspace_trust("/tmp/proj") is True
    assert port.calls[0][0] == "workspace_trust"


@pytest.mark.asyncio
async def test_await_approval_times_out_to_deny() -> None:
    class SlowPort:
        async def request(
            self,
            tool: str,
            args: dict[str, Any],
            preview: Optional[str] = None,
        ) -> Approval:
            import asyncio

            await asyncio.sleep(5)
            return Approval(allowed=True)

    allowed = await await_approval(
        SlowPort(),
        "write_file",
        {"path": "x"},
        timeout_s=0.01,
    )
    assert allowed is False


def test_tui_and_headless_share_approval_port_signature() -> None:
    from coderAI.tui.controller import UIBridge

    deny_params = tuple(inspect.signature(DenyByDefaultApprovalPort.request).parameters)
    bridge_params = tuple(inspect.signature(UIBridge.request).parameters)
    assert deny_params == bridge_params == ("self", "tool", "args", "preview")


def test_coderai_package_has_no_ipc_server_identifier() -> None:
    root = Path(__file__).resolve().parents[2] / "coderAI"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "ipc_server" in code:
                hits.append(f"{path.relative_to(root.parent)}:{lineno}:{line.strip()}")
    assert hits == []


def test_core_does_not_import_tui() -> None:
    root = Path(__file__).resolve().parents[2] / "coderAI" / "core"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name == "coderAI.tui" or name.startswith("coderAI.tui.") for name in imports)


def test_orchestrators_do_not_duck_type_agent_runtime() -> None:
    root = Path(__file__).resolve().parents[2] / "coderAI" / "core"
    for filename in (
        "agent.py",
        "agent_loop.py",
        "agent_capabilities.py",
        "agent_session.py",
        "agent_llm_phase.py",
        "agent_tools_phase.py",
        "agent_finish_reason.py",
        "agent_recovery.py",
        "tool_executor.py",
    ):
        text = (root / filename).read_text(encoding="utf-8")
        assert "vars(self.agent)" not in text
        assert "getattr(self.agent," not in text
        assert "getattr(self," not in text
        assert "hasattr(self.agent," not in text
        assert "hasattr(self," not in text


def test_extracted_loop_phases_have_no_file_wide_mypy_escape_hatch() -> None:
    root = Path(__file__).resolve().parents[2] / "coderAI" / "core"
    for filename in (
        "agent_llm_phase.py",
        "agent_tools_phase.py",
        "agent_finish_reason.py",
        "agent_recovery.py",
    ):
        text = (root / filename).read_text(encoding="utf-8")
        assert "# mypy: disable-error-code" not in text


def test_phase4_modules_are_in_the_append_only_strict_ratchet() -> None:
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    for module in (
        "coderAI.core.agent",
        "coderAI.core.agent_loop",
        "coderAI.core.agent_capabilities",
        "coderAI.core.agent_session",
        "coderAI.core.agent_llm_phase",
        "coderAI.core.agent_tools_phase",
        "coderAI.core.agent_finish_reason",
        "coderAI.core.agent_recovery",
        "coderAI.core.agent_loop_outcomes",
    ):
        assert f'  "{module}",' in pyproject


def test_runtime_view_owns_partial_agent_defaults() -> None:
    partial = SimpleNamespace(read_cache=object(), _workspace_trusted=True)

    runtime = RuntimeView(cast(AgentRuntime, partial))

    assert runtime.read_cache is partial.read_cache
    assert runtime.workspace_trusted is True
    assert runtime.session is None
    assert runtime.hooks_manager is None
    assert runtime.delegation_depth == 0
    assert runtime.mcp_health_task is None


def test_optional_async_close_boundary_is_structural() -> None:
    class Closable:
        async def close(self) -> None:
            return None

    class NotClosable:
        pass

    assert isinstance(Closable(), AsyncClosePort)
    assert not isinstance(NotClosable(), AsyncClosePort)


def test_agent_runtime_protocol_is_structural() -> None:
    assert AgentRuntime.__name__ == "AgentRuntime"
    assert ApprovalPort.__name__ == "ApprovalPort"
    assert "allowed" in Approval.__dataclass_fields__
