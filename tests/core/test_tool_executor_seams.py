"""Focused contracts for ToolExecutor responsibility seams."""

from coderAI.core.tool_batch_scheduler import BatchScheduler
from coderAI.core.tool_confirmation import ConfirmationGate
from coderAI.core.tool_executor import ToolExecutor
from coderAI.core.tool_transaction import TransactionBracket


def test_tool_executor_composes_each_execution_boundary() -> None:
    assert issubclass(ToolExecutor, ConfirmationGate)
    assert issubclass(ToolExecutor, BatchScheduler)
    assert issubclass(ToolExecutor, TransactionBracket)
    assert "_confirmation_callback" in ConfirmationGate.__dict__
    assert "run_tool_batch" in BatchScheduler.__dict__
    assert "_open_workspace_transaction" in TransactionBracket.__dict__
    assert "_finalize_workspace_transaction" in TransactionBracket.__dict__


def test_legacy_private_methods_resolve_on_public_executor() -> None:
    for name in (
        "_confirmation_callback",
        "run_tool_batch",
        "_open_workspace_transaction",
        "_finalize_workspace_transaction",
    ):
        assert callable(getattr(ToolExecutor, name))
