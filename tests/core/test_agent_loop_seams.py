"""Focused contracts for ExecutionLoop phase seams."""

from coderAI.core.agent_finish_reason import FinishReasonHandler
from coderAI.core.agent_llm_phase import LLMPhase
from coderAI.core.agent_loop import ExecutionLoop, RECOVERABLE_ERROR_MARKER
from coderAI.core.agent_recovery import RecoveryHandler
from coderAI.core.agent_tools_phase import ToolsPhase


def test_execution_loop_composes_each_phase() -> None:
    assert issubclass(ExecutionLoop, LLMPhase)
    assert issubclass(ExecutionLoop, ToolsPhase)
    assert issubclass(ExecutionLoop, FinishReasonHandler)
    assert issubclass(ExecutionLoop, RecoveryHandler)
    assert "_handle_llm_phase" in LLMPhase.__dict__
    assert "_handle_tools_phase" in ToolsPhase.__dict__
    assert "_handle_finish_reason" in FinishReasonHandler.__dict__
    assert "_handle_recoverable_error" in RecoveryHandler.__dict__


def test_loop_compatibility_exports_survive_phase_split() -> None:
    assert RECOVERABLE_ERROR_MARKER == "[Recoverable Error]:"
    for name in (
        "_handle_llm_phase",
        "_handle_tools_phase",
        "_handle_finish_reason",
        "_handle_recoverable_error",
    ):
        assert callable(getattr(ExecutionLoop, name))
