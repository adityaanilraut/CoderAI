"""Offline corpus and checked scorer for progressive capability routing.

Run with ``python -m coderAI.evals.capability_routing``.  The corpus models the
eligible registry *after* persona, permission, platform, dependency, network,
Plan Mode, MCP-health, and delegation filters.  The router is therefore graded
on narrowing authority, never on recreating an upstream policy decision.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
import json
import logging
from typing import Any, Literal, Optional

from coderAI.core.capability_routing import (
    CAPABILITY_CATALOG,
    UNIVERSAL_TOOL_NAMES,
    route_capabilities,
)
from coderAI.tools.base import ToolRegistry
from coderAI.tools.discovery import discover_tools


MatchMode = Literal["exact", "subset"]


@dataclass(frozen=True)
class RoutingEvalCase:
    """One objective, eligible surface, and fail-closed routing expectation."""

    name: str
    objective: str
    capabilities: tuple[str, ...]
    boundary_types: tuple[str, ...] = ()
    required_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()
    required_families: frozenset[str] = frozenset()
    forbidden_families: frozenset[str] = frozenset()
    match_mode: MatchMode = "subset"
    exact_tools: frozenset[str] = frozenset()
    conservative_fallback: bool = False
    eligible_tools: Optional[frozenset[str]] = None
    warm_tools: frozenset[str] = frozenset()
    plan_mode: bool = False
    active_plan: bool = False
    dynamic_mcp_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseScore:
    """Deterministic score for one corpus case."""

    name: str
    passed: bool
    match_mode: MatchMode
    capabilities: tuple[str, ...]
    boundary_types: tuple[str, ...]
    selected_tools: tuple[str, ...]
    false_positives: tuple[str, ...]
    false_negatives: tuple[str, ...]
    conservative_fallback_correct: Optional[bool]
    routed_schema_tokens: int
    baseline_schema_tokens: int

    @property
    def token_savings(self) -> int:
        return self.baseline_schema_tokens - self.routed_schema_tokens

    @property
    def token_savings_percent(self) -> float:
        if self.baseline_schema_tokens <= 0:
            return 0.0
        return 100.0 * self.token_savings / self.baseline_schema_tokens


@dataclass(frozen=True)
class EvaluationThresholds:
    """Blocking calibration budgets for the checked corpus."""

    minimum_accuracy: float = 1.0
    minimum_conservative_fallback_accuracy: float = 1.0
    maximum_false_positives: int = 0
    maximum_false_negatives: int = 0
    minimum_token_savings_percent: float = 50.0


DEFAULT_THRESHOLDS = EvaluationThresholds()


@dataclass(frozen=True)
class EvaluationReport:
    """Aggregate routing accuracy, error, cost, and grouping report."""

    corpus_size: int
    passed_cases: int
    accuracy: float
    conservative_cases: int
    conservative_fallback_accuracy: float
    false_positives: int
    false_negatives: int
    routed_schema_tokens: int
    baseline_schema_tokens: int
    token_savings: int
    token_savings_percent: float
    thresholds: EvaluationThresholds
    threshold_failures: tuple[str, ...]
    groups_by_capability: dict[str, dict[str, Any]]
    groups_by_boundary: dict[str, dict[str, Any]]
    cases: tuple[CaseScore, ...]

    @property
    def passed(self) -> bool:
        return not self.threshold_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "passed": self.passed,
        }


_UNIVERSAL = frozenset(UNIVERSAL_TOOL_NAMES)
_ALL_NATIVE = frozenset(
    {
        *_UNIVERSAL,
        *(tool for spec in CAPABILITY_CATALOG for tool in spec.tools),
        "submit_plan",
        "request_plan_amendment",
    }
)


def _case(
    name: str,
    objective: str,
    capability: str,
    *,
    required: Iterable[str] = (),
    forbidden: Iterable[str] = (),
    required_families: Iterable[str] = (),
    forbidden_families: Iterable[str] = (),
    boundaries: Iterable[str] = (),
    **kwargs: Any,
) -> RoutingEvalCase:
    return RoutingEvalCase(
        name=name,
        objective=objective,
        capabilities=(capability,),
        required_tools=frozenset(required),
        forbidden_tools=frozenset(forbidden),
        required_families=frozenset(required_families),
        forbidden_families=frozenset(forbidden_families),
        boundary_types=tuple(boundaries),
        **kwargs,
    )


ROUTING_EVAL_CORPUS: tuple[RoutingEvalCase, ...] = (
    _case(
        "ordinary_read",
        "Read README.md and summarize it",
        "universal",
        required={"read_file"},
        match_mode="exact",
        exact_tools=_UNIVERSAL,
    ),
    _case(
        "ordinary_search",
        "Search for the parser definition and its call sites",
        "code_search",
        required={"symbol_search", "semantic_search"},
        required_families={"code_search"},
        forbidden_families={"workspace_edit", "execution"},
        forbidden={"write_file", "run_command"},
    ),
    _case(
        "editing",
        "Edit parser.py to replace the broken tokenizer",
        "workspace_edit",
        required={"apply_diff", "write_file", "search_replace"},
        required_families={"workspace_edit"},
        forbidden_families={"browser", "web"},
        forbidden={"browser_navigate"},
    ),
    _case(
        "testing",
        "Run pytest and lint the package",
        "quality",
        required={"run_tests", "lint"},
        required_families={"quality", "execution"},
        forbidden={"write_file"},
    ),
    _case(
        "terminal",
        "Execute the build command in the terminal",
        "execution",
        required={"run_command", "run_background"},
        required_families={"execution"},
        forbidden={"browser_navigate"},
    ),
    _case(
        "git",
        "Inspect the git diff and recent commit history",
        "git",
        required={"git_diff", "git_log"},
        required_families={"git"},
        forbidden={"write_file"},
    ),
    _case(
        "web",
        "Search the web and fetch the official URL",
        "web",
        required={"web_search", "read_url"},
        required_families={"web", "code_search"},
        forbidden={"browser_click"},
    ),
    _case(
        "browser",
        "Use Playwright in the browser to click the form",
        "browser",
        required={"browser_navigate", "browser_click", "browser_type"},
        required_families={"browser"},
        forbidden={"run_applescript"},
    ),
    _case(
        "desktop",
        "Inspect the macOS desktop accessibility UI",
        "desktop",
        required={"get_accessibility_tree", "run_applescript"},
        required_families={"desktop", "code_search"},
        forbidden={"browser_navigate"},
    ),
    _case(
        "packages",
        "Install the missing package dependency with pip",
        "packages",
        required={"package_manager"},
        required_families={"packages"},
        forbidden={"git_commit"},
    ),
    _case(
        "memory",
        "Remember this preference in memory",
        "memory",
        required={"save_memory", "recall_memory"},
        required_families={"memory"},
        forbidden={"write_file"},
    ),
    _case(
        "undo",
        "Undo the last workspace change",
        "undo",
        required={"undo", "undo_history"},
        required_families={"undo"},
        forbidden_families={"workspace_edit"},
        forbidden={"write_file"},
    ),
    _case(
        "vision",
        "Inspect this image and explain the diagram",
        "vision",
        required={"read_image"},
        required_families={"vision", "code_search"},
        forbidden={"browser_screenshot"},
    ),
    _case(
        "context",
        "Pin this file in the context",
        "context",
        required={"manage_context"},
        required_families={"context"},
        forbidden={"save_memory"},
    ),
    _case(
        "mcp_control",
        "List MCP resources and read one resource",
        "mcp_control",
        required={"mcp_list_resources", "mcp_read_resource"},
        required_families={"mcp_control"},
        forbidden={"write_file"},
    ),
    RoutingEvalCase(
        name="multi_edit_test_git",
        objective="Implement the fix, run tests, and inspect the git diff",
        capabilities=("workspace_edit", "quality", "git"),
        required_tools=frozenset({"write_file", "run_tests", "git_diff"}),
        required_families=frozenset({"workspace_edit", "quality", "git", "execution"}),
        forbidden_tools=frozenset({"browser_navigate", "web_search"}),
    ),
    RoutingEvalCase(
        name="multi_web_browser_download",
        objective="Search online, browse the website, and download the file",
        capabilities=("web", "browser"),
        required_tools=frozenset({"web_search", "browser_navigate", "download_file"}),
        required_families=frozenset({"web", "browser", "code_search"}),
        forbidden_tools=frozenset({"write_file"}),
    ),
    RoutingEvalCase(
        name="multi_context_memory",
        objective="Pin the context and remember this preference",
        capabilities=("context", "memory"),
        required_tools=frozenset({"manage_context", "save_memory"}),
        required_families=frozenset({"context", "memory"}),
        forbidden_tools=frozenset({"run_command"}),
    ),
    _case(
        "unknown_fallback",
        "Handle frobnicator quux",
        "unknown",
        match_mode="exact",
        exact_tools=_UNIVERSAL,
        conservative_fallback=True,
        forbidden_families={"workspace_edit", "execution", "web"},
        forbidden={"write_file", "run_command", "web_search"},
        boundaries={"unknown"},
    ),
    _case(
        "ambiguous_mutation_fallback",
        "Please fix it",
        "unknown",
        match_mode="exact",
        exact_tools=_UNIVERSAL,
        conservative_fallback=True,
        forbidden={"write_file", "run_command"},
        boundaries={"ambiguous"},
    ),
    _case(
        "persona_ceiling",
        "Edit the file and browse the page",
        "workspace_edit",
        required={"read_file"},
        forbidden={"write_file", "browser_navigate"},
        eligible_tools=_UNIVERSAL | {"run_tests"},
        boundaries={"persona"},
    ),
    _case(
        "plan_mode_ceiling",
        "Plan how to edit and test parser.py",
        "plan_mode",
        required={"read_file", "submit_plan"},
        forbidden={"write_file", "run_tests", "request_plan_amendment"},
        eligible_tools=frozenset({"read_file", "grep", "submit_plan"}),
        plan_mode=True,
        boundaries={"plan_mode"},
    ),
    _case(
        "amendment_boundary",
        "Change the approved implementation plan",
        "amendment",
        required={"request_plan_amendment", "write_file"},
        forbidden={"submit_plan"},
        active_plan=True,
        boundaries={"amendment"},
    ),
    _case(
        "subagent_ceiling",
        "Browse online and write the fix",
        "subagent",
        required={"read_file"},
        forbidden={"browser_navigate", "web_search", "write_file"},
        eligible_tools=_UNIVERSAL | {"symbol_search", "run_tests"},
        boundaries={"subagent"},
    ),
    _case(
        "optional_dependency_ceiling",
        "Use Playwright in the browser",
        "browser",
        required={"read_file"},
        forbidden={"browser_navigate", "browser_snapshot"},
        eligible_tools=_ALL_NATIVE
        - frozenset(tool for tool in _ALL_NATIVE if tool.startswith("browser_")),
        boundaries={"optional_dependency"},
    ),
    _case(
        "platform_ceiling",
        "Inspect the desktop accessibility UI",
        "desktop",
        required={"read_file"},
        forbidden={"run_applescript", "get_accessibility_tree"},
        eligible_tools=_ALL_NATIVE
        - {"run_applescript", "get_accessibility_tree", "click_ui_element", "type_keystrokes"},
        boundaries={"platform"},
    ),
    _case(
        "network_ceiling",
        "Search the web and download the URL",
        "web",
        required={"read_file"},
        forbidden={"web_search", "read_url", "download_file", "http_request"},
        eligible_tools=_ALL_NATIVE - {"web_search", "read_url", "download_file", "http_request"},
        boundaries={"network"},
    ),
    _case(
        "dynamic_mcp_identifier",
        "Use the weather MCP forecast tool",
        "dynamic_mcp",
        required={"mcp__weather__forecast"},
        forbidden={"mcp__calendar__events", "mcp__hostile__unrelated"},
        dynamic_mcp_tools=(
            "mcp__weather__forecast",
            "mcp__calendar__events",
            "mcp__hostile__unrelated",
        ),
        boundaries={"dynamic_mcp"},
    ),
    _case(
        "mcp_health_ceiling",
        "Use the weather MCP forecast tool",
        "dynamic_mcp",
        forbidden={"mcp__weather__forecast"},
        dynamic_mcp_tools=("mcp__calendar__events",),
        boundaries={"mcp_health"},
    ),
    _case(
        "warm_retention",
        "Summarize the command result",
        "warmth",
        required={"run_command"},
        warm_tools={"run_command"},
        boundaries={"same_objective"},
    ),
    _case(
        "warm_objective_reset",
        "Summarize the result",
        "warmth",
        forbidden={"run_command"},
        boundaries={"objective_reset"},
    ),
    _case(
        "warm_session_reset",
        "Summarize the result",
        "warmth",
        forbidden={"run_command"},
        boundaries={"session_reset"},
    ),
    _case(
        "warm_agent_reset",
        "Summarize the result",
        "warmth",
        forbidden={"run_command"},
        boundaries={"agent_reset"},
    ),
    _case(
        "warm_permission_ceiling",
        "Summarize the result",
        "warmth",
        forbidden={"write_file"},
        warm_tools={"write_file"},
        eligible_tools=_ALL_NATIVE - {"write_file"},
        boundaries={"permission"},
    ),
    _case(
        "warm_persona_ceiling",
        "Summarize the result",
        "warmth",
        forbidden={"run_command"},
        warm_tools={"run_command"},
        eligible_tools=_UNIVERSAL | {"run_tests"},
        boundaries={"persona_reset"},
    ),
    _case(
        "warm_plan_transition",
        "Plan the next edit",
        "warmth",
        required={"submit_plan"},
        forbidden={"write_file"},
        warm_tools={"write_file"},
        eligible_tools=frozenset({"read_file", "grep", "submit_plan"}),
        plan_mode=True,
        boundaries={"plan_mode_transition"},
    ),
    _case(
        "warm_dependency_ceiling",
        "Summarize the browser result",
        "warmth",
        forbidden={"browser_navigate"},
        warm_tools={"browser_navigate"},
        eligible_tools=_ALL_NATIVE
        - frozenset(tool for tool in _ALL_NATIVE if tool.startswith("browser_")),
        boundaries={"dependency_reset"},
    ),
    _case(
        "warm_platform_ceiling",
        "Summarize the desktop result",
        "warmth",
        forbidden={"run_applescript"},
        warm_tools={"run_applescript"},
        eligible_tools=_ALL_NATIVE - {"run_applescript"},
        boundaries={"platform_reset"},
    ),
    _case(
        "warm_mcp_health_ceiling",
        "Summarize the forecast result",
        "warmth",
        forbidden={"mcp__weather__forecast"},
        warm_tools={"mcp__weather__forecast"},
        dynamic_mcp_tools=("mcp__calendar__events",),
        boundaries={"mcp_health_reset"},
    ),
    _case(
        "warm_delegated_ceiling",
        "Summarize the delegated result",
        "warmth",
        forbidden={"write_file", "mcp__weather__forecast"},
        warm_tools={"write_file", "mcp__weather__forecast"},
        eligible_tools=_UNIVERSAL | {"symbol_search"},
        boundaries={"delegated_agent_reset"},
    ),
)


def _fallback_schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"CoderAI tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def load_evaluation_schemas() -> dict[str, dict[str, Any]]:
    """Load real native schemas, filling constructor-bound control schemas."""
    registry = ToolRegistry()
    discover_tools(registry)
    schemas = {schema["function"]["name"]: schema for schema in registry.get_schemas()}
    for name in _ALL_NATIVE:
        schemas.setdefault(name, _fallback_schema(name))
    return schemas


def count_schema_tokens(schemas: Sequence[dict[str, Any]]) -> int:
    """Count serialized schema tokens with a pinned, provider-neutral encoding."""
    import tiktoken

    payload = json.dumps(list(schemas), sort_keys=True, separators=(",", ":"))
    return len(tiktoken.get_encoding("cl100k_base").encode(payload))


def _schemas_for_names(
    names: Iterable[str], schemas: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [schemas.get(name, _fallback_schema(name)) for name in sorted(set(names))]


def score_case(
    case: RoutingEvalCase,
    *,
    schemas: dict[str, dict[str, Any]],
    token_counter: Any = count_schema_tokens,
) -> CaseScore:
    """Route and score one case against required/forbidden expectations."""
    eligible_names = case.eligible_tools if case.eligible_tools is not None else _ALL_NATIVE
    native = _schemas_for_names(eligible_names, schemas)
    dynamic = _schemas_for_names(case.dynamic_mcp_tools, schemas)
    decision = route_capabilities(
        objective=case.objective,
        native_schemas=native,
        mcp_schemas=dynamic,
        warm_tool_names=case.warm_tools,
        plan_mode=case.plan_mode,
        active_plan=case.active_plan,
    )
    selected = frozenset(decision.selected_names)
    families = frozenset(decision.matched_capabilities)
    if case.match_mode == "exact":
        false_positives = selected - case.exact_tools
        false_negatives = case.exact_tools - selected
    else:
        false_positives = selected & case.forbidden_tools
        false_negatives = case.required_tools - selected
    family_fp = case.forbidden_families & families
    family_fn = case.required_families - families
    false_positives |= {f"family:{name}" for name in family_fp}
    false_negatives |= {f"family:{name}" for name in family_fn}
    fallback_correct: Optional[bool] = None
    if case.conservative_fallback:
        expected = _UNIVERSAL & eligible_names
        fallback_correct = selected == expected and not decision.selection_success

    baseline = native + dynamic
    passed = not false_positives and not false_negatives and fallback_correct is not False
    return CaseScore(
        name=case.name,
        passed=passed,
        match_mode=case.match_mode,
        capabilities=case.capabilities,
        boundary_types=case.boundary_types,
        selected_tools=decision.selected_names,
        false_positives=tuple(sorted(false_positives)),
        false_negatives=tuple(sorted(false_negatives)),
        conservative_fallback_correct=fallback_correct,
        routed_schema_tokens=token_counter(list(decision.schemas)),
        baseline_schema_tokens=token_counter(baseline),
    )


def _group_scores(scores: Sequence[CaseScore], attribute: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CaseScore]] = defaultdict(list)
    for score in scores:
        for label in getattr(score, attribute):
            grouped[label].append(score)
    result: dict[str, dict[str, Any]] = {}
    for label, items in sorted(grouped.items()):
        baseline = sum(item.baseline_schema_tokens for item in items)
        routed = sum(item.routed_schema_tokens for item in items)
        result[label] = {
            "cases": len(items),
            "accuracy": sum(item.passed for item in items) / len(items),
            "false_positives": sum(len(item.false_positives) for item in items),
            "false_negatives": sum(len(item.false_negatives) for item in items),
            "routed_schema_tokens": routed,
            "baseline_schema_tokens": baseline,
            "token_savings_percent": 100.0 * (baseline - routed) / baseline if baseline else 0.0,
        }
    return result


def evaluate_corpus(
    corpus: Sequence[RoutingEvalCase] = ROUTING_EVAL_CORPUS,
    *,
    schemas: Optional[dict[str, dict[str, Any]]] = None,
    thresholds: EvaluationThresholds = DEFAULT_THRESHOLDS,
    token_counter: Any = count_schema_tokens,
) -> EvaluationReport:
    """Score the corpus and enforce checked accuracy and schema-cost budgets."""
    available = schemas if schemas is not None else load_evaluation_schemas()
    scores = tuple(
        score_case(case, schemas=available, token_counter=token_counter) for case in corpus
    )
    count = len(scores)
    passed_cases = sum(score.passed for score in scores)
    accuracy = passed_cases / count if count else 0.0
    conservative = [score for score in scores if score.conservative_fallback_correct is not None]
    fallback_accuracy = (
        sum(score.conservative_fallback_correct is True for score in conservative)
        / len(conservative)
        if conservative
        else 1.0
    )
    false_positives = sum(len(score.false_positives) for score in scores)
    false_negatives = sum(len(score.false_negatives) for score in scores)
    routed = sum(score.routed_schema_tokens for score in scores)
    baseline = sum(score.baseline_schema_tokens for score in scores)
    savings = baseline - routed
    savings_percent = 100.0 * savings / baseline if baseline else 0.0
    failures: list[str] = []
    if accuracy < thresholds.minimum_accuracy:
        failures.append(f"accuracy {accuracy:.2%} < {thresholds.minimum_accuracy:.2%}")
    if fallback_accuracy < thresholds.minimum_conservative_fallback_accuracy:
        failures.append(
            "conservative fallback accuracy "
            f"{fallback_accuracy:.2%} < "
            f"{thresholds.minimum_conservative_fallback_accuracy:.2%}"
        )
    if false_positives > thresholds.maximum_false_positives:
        failures.append(f"false positives {false_positives} > {thresholds.maximum_false_positives}")
    if false_negatives > thresholds.maximum_false_negatives:
        failures.append(f"false negatives {false_negatives} > {thresholds.maximum_false_negatives}")
    if savings_percent < thresholds.minimum_token_savings_percent:
        failures.append(
            "schema token savings "
            f"{savings_percent:.2f}% < {thresholds.minimum_token_savings_percent:.2f}%"
        )
    return EvaluationReport(
        corpus_size=count,
        passed_cases=passed_cases,
        accuracy=accuracy,
        conservative_cases=len(conservative),
        conservative_fallback_accuracy=fallback_accuracy,
        false_positives=false_positives,
        false_negatives=false_negatives,
        routed_schema_tokens=routed,
        baseline_schema_tokens=baseline,
        token_savings=savings,
        token_savings_percent=savings_percent,
        thresholds=thresholds,
        threshold_failures=tuple(failures),
        groups_by_capability=_group_scores(scores, "capabilities"),
        groups_by_boundary=_group_scores(scores, "boundary_types"),
        cases=scores,
    )


def _text_report(report: EvaluationReport) -> str:
    lines = [
        f"Routing evaluation: {'PASS' if report.passed else 'FAIL'}",
        f"Corpus: {report.passed_cases}/{report.corpus_size} cases "
        f"({report.accuracy:.2%} accuracy)",
        "Conservative fallback: "
        f"{report.conservative_fallback_accuracy:.2%} across {report.conservative_cases} cases",
        f"False positives / negatives: {report.false_positives} / {report.false_negatives}",
        f"Schema tokens: {report.routed_schema_tokens} routed vs "
        f"{report.baseline_schema_tokens} eligible-registry baseline",
        f"Savings: {report.token_savings} ({report.token_savings_percent:.2f}%)",
        "Capability groups:",
    ]
    for name, group in report.groups_by_capability.items():
        lines.append(
            f"- {name}: {group['cases']} cases, {group['accuracy']:.2%} accuracy, "
            f"{group['token_savings_percent']:.2f}% savings"
        )
    lines.append("Boundary groups:")
    for name, group in report.groups_by_boundary.items():
        lines.append(
            f"- {name}: {group['cases']} cases, {group['accuracy']:.2%} accuracy, "
            f"{group['token_savings_percent']:.2f}% savings"
        )
    failed_cases = [case for case in report.cases if not case.passed]
    for case in failed_cases:
        lines.append(
            f"- {case.name}: FP={list(case.false_positives)} FN={list(case.false_negatives)}"
        )
    for failure in report.threshold_failures:
        lines.append(f"- threshold: {failure}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate deterministic capability routing")
    parser.add_argument("--json", action="store_true", help="emit the full JSON report")
    parser.add_argument(
        "--no-check", action="store_true", help="report regressions without a non-zero exit"
    )
    args = parser.parse_args(argv)
    logging.getLogger("coderAI.tools.discovery").setLevel(logging.ERROR)
    report = evaluate_corpus()
    print(json.dumps(report.as_dict(), sort_keys=True) if args.json else _text_report(report))
    return 0 if args.no_check or report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
