"""Checked offline evaluation coverage for progressive schema routing."""

from __future__ import annotations

from coderAI.core.capability_routing import UNIVERSAL_TOOL_NAMES
from coderAI.evals.capability_routing import (
    DEFAULT_THRESHOLDS,
    ROUTING_EVAL_CORPUS,
    EvaluationThresholds,
    RoutingEvalCase,
    evaluate_corpus,
    score_case,
)


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_checked_corpus_meets_accuracy_fallback_and_token_budgets() -> None:
    report = evaluate_corpus()

    assert len(ROUTING_EVAL_CORPUS) == 40
    assert report.passed is True
    assert report.accuracy == DEFAULT_THRESHOLDS.minimum_accuracy == 1.0
    assert report.conservative_fallback_accuracy == 1.0
    assert report.false_positives == 0
    assert report.false_negatives == 0
    assert report.token_savings_percent >= 70.0
    assert report.routed_schema_tokens < report.baseline_schema_tokens
    assert "workspace_edit" in report.groups_by_capability
    assert "plan_mode_transition" in report.groups_by_boundary


def test_scorer_reports_exact_and_subset_false_positives_and_negatives() -> None:
    schemas = {name: _schema(name) for name in (*UNIVERSAL_TOOL_NAMES, "write_file")}
    exact = RoutingEvalCase(
        name="exact",
        objective="handle quux",
        capabilities=("unknown",),
        match_mode="exact",
        exact_tools=frozenset({"read_file"}),
        eligible_tools=frozenset({"read_file", "grep"}),
    )
    exact_score = score_case(exact, schemas=schemas, token_counter=len)
    assert exact_score.false_positives == ("grep",)
    assert exact_score.false_negatives == ()

    subset = RoutingEvalCase(
        name="subset",
        objective="write parser.py",
        capabilities=("workspace_edit",),
        required_tools=frozenset({"write_file", "missing_tool"}),
        forbidden_tools=frozenset({"write_file"}),
        eligible_tools=frozenset({"read_file", "write_file"}),
    )
    subset_score = score_case(subset, schemas=schemas, token_counter=len)
    assert subset_score.false_positives == ("write_file",)
    assert subset_score.false_negatives == ("missing_tool",)


def test_threshold_regressions_are_blocking() -> None:
    case = RoutingEvalCase(
        name="regression",
        objective="write parser.py",
        capabilities=("workspace_edit",),
        forbidden_tools=frozenset({"write_file"}),
        eligible_tools=frozenset({"read_file", "write_file"}),
    )
    report = evaluate_corpus(
        [case],
        schemas={"read_file": _schema("read_file"), "write_file": _schema("write_file")},
        thresholds=EvaluationThresholds(minimum_token_savings_percent=99.0),
        token_counter=len,
    )

    assert report.passed is False
    assert any("accuracy" in failure for failure in report.threshold_failures)
    assert any("false positives" in failure for failure in report.threshold_failures)
    assert any("schema token savings" in failure for failure in report.threshold_failures)


def test_corpus_covers_every_declared_routing_boundary() -> None:
    covered = {boundary for case in ROUTING_EVAL_CORPUS for boundary in case.boundary_types}

    assert {
        "ambiguous",
        "amendment",
        "delegated_agent_reset",
        "dependency_reset",
        "dynamic_mcp",
        "mcp_health",
        "mcp_health_reset",
        "network",
        "objective_reset",
        "optional_dependency",
        "permission",
        "persona",
        "persona_reset",
        "plan_mode",
        "plan_mode_transition",
        "platform",
        "platform_reset",
        "session_reset",
        "subagent",
        "unknown",
    } <= covered
