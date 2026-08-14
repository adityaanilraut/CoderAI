"""Deterministic, objective-scoped tool-schema routing.

The live registry remains authoritative for availability and the executor for
permission. Capability membership comes exclusively from the typed tool
semantics catalog; the router can only intersect declared tags with schemas
that are already eligible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import re
from typing import Any

from coderAI.tools.semantics import (
    SEMANTICS_BY_NAME,
    CapabilityTag,
    UNIVERSAL_TOOL_NAMES,
    tools_for_capabilities,
)


UNIVERSAL_SCHEMA_LIMIT = 9
MAX_DYNAMIC_MCP_SCHEMAS = 8


@dataclass(frozen=True)
class CapabilitySpec:
    """A capability tag and its legacy objective-inference vocabulary.

    ``tools`` is a derived compatibility view. Tool membership is declared in
    :mod:`coderAI.tools.semantics`, never in this prompt inference boundary.
    """

    name: CapabilityTag
    keywords: frozenset[str]
    phrases: tuple[str, ...] = ()

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(sorted(tools_for_capabilities(frozenset({self.name}))))


# Existing free-form prompts have no structured intent field. Keep their
# vocabulary compatibility in one explicit boundary, then pass typed tags to
# the router. New callers should declare ``capability_tags`` directly.
CAPABILITY_CATALOG: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        "code_search",
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
                "tree",
            }
        ),
        ("code search", "where is", "call site", "directory tree"),
    ),
    CapabilitySpec(
        "session_context",
        frozenset({"compact", "context", "export", "pins", "transcript", "tokens"}),
        ("context window", "export session", "token usage"),
    ),
    CapabilitySpec(
        "workspace_edit",
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
        frozenset(
            {
                "build",
                "command",
                "debug",
                "execute",
                "input",
                "logs",
                "process",
                "reproduce",
                "run",
                "server",
                "shell",
                "stdin",
                "terminal",
            }
        ),
        ("start the server", "run it", "send input"),
    ),
    CapabilitySpec(
        "quality",
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
        frozenset(
            {"branch", "commit", "diff", "git", "history", "merge", "rebase", "stage", "tag"}
        ),
        ("cherry pick", "pull request"),
    ),
    CapabilitySpec(
        "web",
        frozenset({"download", "fetch", "http", "internet", "online", "url", "web"}),
        ("look online", "search the web"),
    ),
    CapabilitySpec(
        "browser",
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
        frozenset(
            {"accessibility", "applescript", "desktop", "keystroke", "macos", "ui"}
        ),
        ("user interface", "desktop app"),
    ),
    CapabilitySpec(
        "packages",
        frozenset(
            {"dependency", "dependencies", "install", "package", "pip", "poetry", "upgrade"}
        ),
    ),
    CapabilitySpec(
        "memory",
        frozenset({"forget", "memory", "recall", "remember"}),
    ),
    CapabilitySpec("undo", frozenset({"revert", "rollback", "undo", "rewind"})),
    CapabilitySpec(
        "vision",
        frozenset({"diagram", "image", "photo", "picture", "visual"}),
        ("look at this image",),
    ),
    CapabilitySpec(
        "context",
        frozenset({"context", "pin", "unpin"}),
        ("pinned file",),
    ),
    CapabilitySpec(
        "mcp_control",
        frozenset({"mcp"}),
        ("model context protocol",),
    ),
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_BROAD_MUTATION_WORDS = frozenset(
    {
        "add",
        "change",
        "create",
        "delete",
        "edit",
        "fix",
        "implement",
        "modify",
        "patch",
        "update",
    }
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
class InferredCapabilities:
    """Compatibility result for a legacy free-form objective."""

    tags: frozenset[CapabilityTag]
    ambiguous: bool = False


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
    if informative:
        has_path_signal = any(
            (
                "/" in token
                or "." in token
                or token in {"file", "function", "class", "method", "module"}
            )
            for token in tokens
        )
        if not has_path_signal and len(informative) <= 2:
            return True
        return False
    return True


def infer_capability_tags(objective: str) -> InferredCapabilities:
    """Migrate a legacy free-form objective to declared capability tags."""
    normalized = " ".join(_TOKEN_RE.findall((objective or "").lower()))
    tokens = set(normalized.split())
    matched = [spec for spec in CAPABILITY_CATALOG if _matches(spec, normalized, tokens)]
    if any(spec.name == "undo" for spec in matched):
        matched = [spec for spec in matched if spec.name != "workspace_edit"]
    ambiguous = _is_ambiguous_mutation(tokens, matched)
    if ambiguous:
        matched = []
    return InferredCapabilities(frozenset(spec.name for spec in matched), ambiguous)


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
    capability_tags: Iterable[CapabilityTag] | None = None,
    mcp_schemas: Iterable[dict[str, Any]] = (),
    warm_tool_names: Iterable[str] = (),
    plan_mode: bool = False,
    active_plan: bool = False,
) -> RoutingDecision:
    """Return the eligible schema subset selected by declared capability tags.

    ``capability_tags`` is the typed route. Omitting it retains compatibility
    for callers that only have a free-form objective by using the isolated
    :func:`infer_capability_tags` migration boundary.
    """
    if len(UNIVERSAL_TOOL_NAMES) >= 10 or len(UNIVERSAL_TOOL_NAMES) > UNIVERSAL_SCHEMA_LIMIT:
        raise RuntimeError("Universal capability catalog must remain below ten schemas")

    native = _dedupe_schemas(native_schemas)
    dynamic = _dedupe_schemas(mcp_schemas)
    warm = set(warm_tool_names)
    objective_tokens = set(_TOKEN_RE.findall((objective or "").lower()))
    if capability_tags is None:
        inferred = infer_capability_tags(objective)
        tags = inferred.tags
        ambiguous = inferred.ambiguous
    else:
        tags = frozenset(capability_tags)
        ambiguous = False

    selected: set[str] = {name for name in UNIVERSAL_TOOL_NAMES if name in native}
    selected.update(name for name in tools_for_capabilities(tags) if name in native)

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
        selected_dynamic = _select_dynamic_mcp(objective, objective_tokens, dynamic, warm)

    native_names = [name for name in native if name in selected]
    dynamic_names = [name for name in dynamic if name in selected_dynamic]
    schemas = tuple(native[name] for name in native_names) + tuple(
        dynamic[name] for name in dynamic_names
    )
    selected_names = tuple(native_names + dynamic_names)

    matched_names = tuple(sorted(tags))
    reasons: list[str] = []
    if matched_names:
        source = "declared" if capability_tags is not None else "objective"
        reasons.append(source + ":" + ",".join(matched_names))
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


def validate_catalog_against_registry(tool_names: Iterable[str]) -> tuple[str, ...]:
    """Return registered native tools missing an explicit semantics row."""
    return tuple(sorted(name for name in tool_names if name not in SEMANTICS_BY_NAME))
