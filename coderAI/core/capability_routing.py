"""Deterministic, objective-scoped tool-schema routing.

The registry remains the authority for which tools exist and the executor remains
the authority for whether a call may run.  This module only narrows the schemas
shown to a model for one objective; it can never manufacture or re-enable a tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import re
from typing import Any


# These are the only schemas shown on an otherwise unclassified ordinary turn.
# Keep this list deliberately small: objective-specific families are added below.
UNIVERSAL_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "grep",
    "glob_search",
    "list_directory",
    "git_status",
    "manage_tasks",
    "delegate_task",
    "use_skill",
)
UNIVERSAL_SCHEMA_LIMIT = 9
MAX_DYNAMIC_MCP_SCHEMAS = 8


@dataclass(frozen=True)
class CapabilitySpec:
    """One compact native capability family and its objective vocabulary."""

    name: str
    tools: tuple[str, ...]
    keywords: frozenset[str]
    phrases: tuple[str, ...] = ()


CAPABILITY_CATALOG: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "code_search",
        ("symbol_search", "semantic_search", "file_stat", "file_readlink"),
        frozenset(
            {
                "analyze",
                "analyse",
                "architecture",
                "definition",
                "explain",
                "find",
                "inspect",
                "investigate",
                "locate",
                "reference",
                "review",
                "search",
                "symbol",
                "trace",
            }
        ),
        ("code search", "where is", "call site"),
    ),
    CapabilitySpec(
        "workspace_edit",
        (
            "apply_diff",
            "write_file",
            "search_replace",
            "create_directory",
            "move_file",
            "copy_file",
            "delete_file",
            "file_chmod",
            "refactor",
        ),
        frozenset(
            {
                "add",
                "change",
                "create",
                "delete",
                "edit",
                "fix",
                "implement",
                "modify",
                "move",
                "patch",
                "refactor",
                "remove",
                "rename",
                "replace",
                "update",
                "write",
            }
        ),
    ),
    CapabilitySpec(
        "execution",
        (
            "run_command",
            "run_background",
            "read_bg_output",
            "list_processes",
            "kill_process",
            "python_repl",
        ),
        frozenset(
            {
                "build",
                "command",
                "debug",
                "execute",
                "logs",
                "process",
                "reproduce",
                "run",
                "server",
                "shell",
                "terminal",
            }
        ),
        ("start the server", "run it"),
    ),
    CapabilitySpec(
        "quality",
        ("run_tests", "lint", "format"),
        frozenset(
            {
                "check",
                "ci",
                "format",
                "lint",
                "mypy",
                "pytest",
                "ruff",
                "test",
                "tests",
                "typecheck",
                "validate",
                "verification",
                "verify",
            }
        ),
        ("type check", "quality gate"),
    ),
    CapabilitySpec(
        "git",
        ("git_diff", "git_log", "git_add", "git_commit", "git_branch"),
        frozenset(
            {
                "branch",
                "commit",
                "diff",
                "git",
                "history",
                "merge",
                "rebase",
                "stage",
                "tag",
            }
        ),
        ("cherry pick", "pull request"),
    ),
    CapabilitySpec(
        "web",
        ("web_search", "read_url", "download_file", "http_request"),
        frozenset(
            {
                "download",
                "fetch",
                "http",
                "internet",
                "online",
                "url",
                "web",
            }
        ),
        ("look online", "search the web"),
    ),
    CapabilitySpec(
        "browser",
        (
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_select_option",
            "browser_get_content",
            "browser_screenshot",
            "browser_evaluate",
            "browser_wait",
            "browser_close",
        ),
        frozenset(
            {
                "browser",
                "chromium",
                "click",
                "dom",
                "form",
                "page",
                "playwright",
                "screenshot",
                "website",
            }
        ),
        ("web page", "fill out"),
    ),
    CapabilitySpec(
        "desktop",
        (
            "run_applescript",
            "get_accessibility_tree",
            "click_ui_element",
            "type_keystrokes",
        ),
        frozenset(
            {
                "accessibility",
                "applescript",
                "desktop",
                "keystroke",
                "macos",
                "ui",
            }
        ),
        ("user interface", "desktop app"),
    ),
    CapabilitySpec(
        "packages",
        ("package_manager",),
        frozenset(
            {
                "dependency",
                "dependencies",
                "install",
                "package",
                "pip",
                "poetry",
                "upgrade",
            }
        ),
    ),
    CapabilitySpec(
        "memory",
        ("save_memory", "recall_memory", "delete_memory"),
        frozenset({"forget", "memory", "recall", "remember"}),
    ),
    CapabilitySpec(
        "undo",
        ("undo", "undo_history"),
        frozenset({"revert", "rollback", "undo", "rewind"}),
    ),
    CapabilitySpec(
        "vision",
        ("read_image",),
        frozenset({"diagram", "image", "photo", "picture", "visual"}),
        ("look at this image",),
    ),
    CapabilitySpec(
        "context",
        ("manage_context",),
        frozenset({"context", "pin", "unpin"}),
        ("pinned file",),
    ),
    CapabilitySpec(
        "mcp_control",
        (
            "mcp_connect",
            "mcp_disconnect",
            "mcp_list",
            "mcp_list_resources",
            "mcp_read_resource",
            "mcp_list_prompts",
            "mcp_get_prompt",
        ),
        frozenset({"mcp"}),
        ("model context protocol",),
    ),
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_BROAD_MUTATION_WORDS = frozenset(
    {"add", "change", "create", "delete", "edit", "fix", "implement", "modify", "patch", "update"}
)
_AMBIGUOUS_REFERENTS = frozenset(
    {
        "code",
        "it",
        "please",
        "project",
        "repo",
        "repository",
        "something",
        "stuff",
        "that",
        "thing",
        "this",
    }
)


@dataclass(frozen=True)
class RoutingDecision:
    """Selected schemas plus compact, event-safe routing evidence."""

    schemas: tuple[dict[str, Any], ...]
    selected_names: tuple[str, ...]
    matched_capabilities: tuple[str, ...]
    routing_reason: str
    selection_success: bool


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _dedupe_schemas(schemas: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for schema in schemas:
        name = _schema_name(schema)
        if name and name not in by_name:
            by_name[name] = schema
    return by_name


def _matches(spec: CapabilitySpec, normalized: str, tokens: set[str]) -> bool:
    return bool(tokens & spec.keywords) or any(phrase in normalized for phrase in spec.phrases)


def _is_ambiguous_mutation(tokens: set[str], matched: Sequence[CapabilitySpec]) -> bool:
    if not any(spec.name == "workspace_edit" for spec in matched):
        return False
    if not (tokens & _BROAD_MUTATION_WORDS):
        return False
    informative = tokens - _BROAD_MUTATION_WORDS - _AMBIGUOUS_REFERENTS
    return not informative


def _identifier_tokens(name: str) -> set[str]:
    return set(_TOKEN_RE.findall(name.lower()))


def _select_dynamic_mcp(
    objective: str,
    objective_tokens: set[str],
    schemas: dict[str, dict[str, Any]],
    warm_names: set[str],
) -> list[str]:
    """Select MCP proxies from trusted identifiers only, never descriptions."""
    normalized = objective.lower()
    scored: list[tuple[int, str]] = []
    for name in schemas:
        if not name.startswith("mcp__"):
            continue
        score = 0
        if name.lower() in normalized:
            score += 100
        identifier_tokens = _identifier_tokens(name) - {"mcp"}
        score += 10 * len(objective_tokens & identifier_tokens)
        if name in warm_names:
            score += 50
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _score, name in scored[:MAX_DYNAMIC_MCP_SCHEMAS]]


def route_capabilities(
    *,
    objective: str,
    native_schemas: Iterable[dict[str, Any]],
    mcp_schemas: Iterable[dict[str, Any]] = (),
    warm_tool_names: Iterable[str] = (),
    plan_mode: bool = False,
    active_plan: bool = False,
) -> RoutingDecision:
    """Return the deterministic schema subset for one objective.

    Inputs must already be filtered by registry, persona, dependency, platform,
    permission-domain, and Plan Mode boundaries.  Warm names are intersected
    with those eligible inputs, so warmth cannot widen authority.
    """
    if len(UNIVERSAL_TOOL_NAMES) >= 10 or len(UNIVERSAL_TOOL_NAMES) > UNIVERSAL_SCHEMA_LIMIT:
        raise RuntimeError("Universal capability catalog must remain below ten schemas")

    native = _dedupe_schemas(native_schemas)
    dynamic = _dedupe_schemas(mcp_schemas)
    warm = set(warm_tool_names)
    normalized = " ".join(_TOKEN_RE.findall((objective or "").lower()))
    tokens = set(normalized.split())
    matched = [spec for spec in CAPABILITY_CATALOG if _matches(spec, normalized, tokens)]
    ambiguous = _is_ambiguous_mutation(tokens, matched)
    if ambiguous:
        matched = []

    selected: set[str] = {name for name in UNIVERSAL_TOOL_NAMES if name in native}
    for spec in matched:
        selected.update(name for name in spec.tools if name in native)

    context_reasons: list[str] = []
    if plan_mode and "submit_plan" in native:
        selected.add("submit_plan")
        context_reasons.append("plan_mode")
    elif active_plan and "request_plan_amendment" in native:
        selected.add("request_plan_amendment")
        context_reasons.append("active_plan")

    warm_native = sorted(warm & native.keys())
    selected.update(warm_native)

    selected_dynamic: list[str] = []
    if not plan_mode:
        selected_dynamic = _select_dynamic_mcp(objective, tokens, dynamic, warm)

    native_names = [name for name in native if name in selected]
    dynamic_names = [name for name in dynamic if name in selected_dynamic]
    schemas = tuple(native[name] for name in native_names) + tuple(
        dynamic[name] for name in dynamic_names
    )
    selected_names = tuple(native_names + dynamic_names)

    matched_names = tuple(spec.name for spec in matched)
    reasons: list[str] = []
    if matched_names:
        reasons.append("objective:" + ",".join(matched_names))
    elif ambiguous:
        reasons.append("conservative_ambiguous")
    else:
        reasons.append("conservative_unknown")
    reasons.extend(context_reasons)
    if warm_native or any(name in warm for name in dynamic_names):
        reasons.append("warm:" + ",".join(sorted(warm & set(selected_names))))
    if dynamic_names:
        reasons.append("dynamic_mcp:" + ",".join(dynamic_names))

    success = bool(matched_names or context_reasons or warm_native or dynamic_names)
    return RoutingDecision(
        schemas=schemas,
        selected_names=selected_names,
        matched_capabilities=matched_names,
        routing_reason=";".join(reasons),
        selection_success=success,
    )
