"""Permission model: confirmation defaults + argument-scoped approval rules.

Phase 4 of the security hardening plan. Two responsibilities:

* :func:`tool_requires_confirmation` — the confirmation-by-default rule (4.1):
  a mutating tool requires confirmation unless it explicitly opts out with
  ``safe = True``. A tool that declares no classification at all is treated as
  requiring confirmation (fail-closed) — forgetting to classify a new mutating
  tool can never silently grant it unattended execution.
* :class:`ApprovalRules` — argument-scoped "always allow" (4.2): an "always
  allow" decision is scoped to a tool *plus* a reviewed command-prefix / path,
  never a bare tool name for high-risk tools. This stops
  ``/allow-tool run_command`` from silently authorizing a *different*
  subsequent command (H6).
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "ApprovalRules",
    "ToolResolver",
    "is_high_risk_no_blanket",
    "tool_requires_confirmation",
]

# Resolver signature: tool name → the live ``Tool`` object (or ``None`` when the
# name isn't a registered local tool). ``ApprovalRules`` reads each tool's
# ``high_risk_no_blanket`` / ``approval_scope`` attributes through this instead
# of a hand-maintained name list (Phase 4.2).
ToolResolver = Callable[[str], Any]

# Static fail-closed fallback used only when no resolver is supplied (tests /
# legacy call sites). It mirrors the attributes declared on the shipped Tool
# classes so behaviour is identical whether or not the registry is available.
# The single source of truth remains the Tool classes; this table exists so a
# bare ``ApprovalRules()`` still refuses blanket-allow for the known high-risk
# tools and scopes them correctly.
_STATIC_SCOPE: Dict[str, str] = {
    "run_command": "command",
    "run_background": "command",
    "write_file": "path",
    "delete_file": "path",
    "move_file": "path",
}
_STATIC_HIGH_RISK = frozenset(
    {
        "run_command",
        "run_background",
        "python_repl",
        "package_manager",
        "write_file",
        "delete_file",
        "move_file",
    }
)

# Shell control operators. A scoped command-prefix rule must never match a
# command that chains, redirects, or substitutes — otherwise a rule allowing
# "git status" would also authorize "git status; rm -rf /".
_SHELL_CONTROL = (";", "&&", "||", "|", "`", "$(", "${", ">", "<", "&", "\n")


def is_high_risk_no_blanket(tool_name: str, resolver: Optional[ToolResolver] = None) -> bool:
    """True if *tool_name* forbids a blanket, name-level "always allow" (Phase 4.2).

    With a *resolver*, reads the tool's ``high_risk_no_blanket`` attribute and
    fails closed (high-risk) for any name the resolver can't map to a Tool.
    Without a resolver, falls back to the static table of shipped high-risk
    tools (unknown names are treated as *not* high-risk so low-risk tools stay
    blanket-allowable in the resolver-less/test path).
    """
    if resolver is not None:
        tool = resolver(tool_name)
        if tool is None:
            return True  # fail-closed: an unresolved tool can't be vouched safe
        return bool(getattr(tool, "high_risk_no_blanket", False))
    return tool_name in _STATIC_HIGH_RISK


def _approval_scope(tool_name: str, resolver: Optional[ToolResolver] = None) -> Optional[str]:
    """Scope kind (``"command"`` / ``"path"`` / ``None``) for *tool_name*."""
    if resolver is not None:
        tool = resolver(tool_name)
        if tool is None:
            return None
        scope = getattr(tool, "approval_scope", None)
        return scope if scope in ("command", "path") else None
    return _STATIC_SCOPE.get(tool_name)


def tool_requires_confirmation(tool: Any) -> bool:
    """Effective confirmation requirement for *tool* (Phase 4.1).

    * ``requires_confirmation = True`` → always confirm.
    * read-only → never (parallel-safe reads).
    * mutating + ``safe = True`` → opt-out, no confirm.
    * mutating + no opt-out → confirm (fail-closed; a tool that forgot to
      classify itself is treated as dangerous).

    Uses ``getattr`` so it stays robust against the lightweight mock tools used
    in tests. ``None`` (e.g. an MCP proxy with no local Tool object) returns
    ``False`` here — the executor gates those separately.
    """
    if tool is None:
        return False
    if getattr(tool, "requires_confirmation", False):
        return True
    if getattr(tool, "is_read_only", False):
        return False
    return not getattr(tool, "safe", False)


def _command_matches_prefix(command: str, prefix: str) -> bool:
    command = (command or "").strip()
    prefix = (prefix or "").strip()
    if not command or not prefix:
        return False
    # A scoped prefix rule must not authorize chaining/redirection/substitution.
    if any(op in command for op in _SHELL_CONTROL):
        return False
    try:
        cmd_tokens = shlex.split(command)
        pfx_tokens = shlex.split(prefix)
    except ValueError:
        return False
    if not pfx_tokens or len(pfx_tokens) > len(cmd_tokens):
        return False
    return cmd_tokens[: len(pfx_tokens)] == pfx_tokens


def _path_matches_scope(path: str, scope: str) -> bool:
    path = (path or "").strip()
    scope = (scope or "").strip()
    if not path or not scope:
        return False
    # Normalize both sides before comparing so a scope of "src" cannot be
    # escaped with ``..`` — e.g. "src/../.coderAI/hooks.json" must NOT match.
    # A raw string-prefix compare would auto-approve those with no prompt.
    norm_scope = os.path.normpath(scope)
    norm_path = os.path.normpath(path)
    # A normalized path that climbed out of (or above) its anchor can never be
    # inside the scope.
    if norm_path == ".." or norm_path.startswith(".." + os.sep):
        return False
    # Mixed anchoring (one absolute, one relative) is not comparable.
    if os.path.isabs(norm_path) != os.path.isabs(norm_scope):
        return False
    if norm_path == norm_scope:
        return True
    # Directory-prefix rule: a scope of "src" authorizes "src/app.py" only when
    # the normalized path still lives under the normalized scope.
    try:
        return os.path.commonpath([norm_scope, norm_path]) == norm_scope
    except ValueError:
        # Different drives (Windows) or mixed anchoring slipped through.
        return False


class ApprovalRules:
    """Session-scoped "always allow" rules, keyed by tool + optional scope.

    Bare-name rules are only accepted for tools whose Tool class does not set
    ``high_risk_no_blanket``. High-risk tools must be either approved on every
    call or scoped to a reviewed command-prefix / path (``approval_scope``).

    A *resolver* (``Agent`` → ``self.tools.get``) supplies the per-tool
    attributes; without one, a static fail-closed fallback covers the shipped
    high-risk tools so tests and legacy call sites behave identically.
    """

    def __init__(self, resolver: Optional[ToolResolver] = None) -> None:
        self._resolver = resolver
        self._names: set[str] = set()
        self._scopes: Dict[str, set[str]] = {}

    def _is_high_risk(self, tool_name: str) -> bool:
        return is_high_risk_no_blanket(tool_name, self._resolver)

    def _scope_kind(self, tool_name: str) -> Optional[str]:
        return _approval_scope(tool_name, self._resolver)

    def allow(self, tool_name: str, scope: Optional[str] = None) -> Tuple[bool, str]:
        """Record an allow rule. Returns ``(accepted, user_message)``."""
        tool_name = (tool_name or "").strip()
        if not tool_name:
            return False, "Usage: /allow-tool <tool-name> [command-prefix | path]"
        scope = (scope or "").strip()
        if scope:
            if self._scope_kind(tool_name) is None:
                return False, (
                    f"'{tool_name}' does not support a scoped allow rule; "
                    "approve each call instead."
                )
            self._scopes.setdefault(tool_name, set()).add(scope)
            return True, f"Scoped approval added: {tool_name} → “{scope}”."
        if self._is_high_risk(tool_name):
            hint = (
                f" Scope it instead: /allow-tool {tool_name} <command-prefix | path>."
                if self._scope_kind(tool_name) is not None
                else " It must be approved on every call."
            )
            return False, (
                f"'{tool_name}' is high-risk — a blanket 'always allow' is refused." + hint
            )
        self._names.add(tool_name)
        return True, f"Approval memory enabled for {tool_name}."

    def disallow(self, tool_name: str) -> None:
        tool_name = (tool_name or "").strip()
        self._names.discard(tool_name)
        self._scopes.pop(tool_name, None)

    def clear(self) -> None:
        self._names.clear()
        self._scopes.clear()

    def is_allowed(self, tool_name: str, arguments: Optional[Dict[str, Any]]) -> bool:
        """True if this exact call is pre-approved by a recorded rule."""
        if tool_name in self._names:
            # High-risk names never enter ``_names`` via ``allow()``; this guard
            # is belt-and-suspenders in case one is injected some other way.
            return not self._is_high_risk(tool_name)
        scopes = self._scopes.get(tool_name)
        if not scopes:
            return False
        args = arguments if isinstance(arguments, dict) else {}
        return any(self._scope_matches(tool_name, s, args) for s in scopes)

    def _scope_matches(self, tool_name: str, scope: str, args: Dict[str, Any]) -> bool:
        kind = self._scope_kind(tool_name)
        if kind == "command":
            return _command_matches_prefix(str(args.get("command", "")), scope)
        if kind == "path":
            path = args.get("path") or args.get("file_path") or ""
            return _path_matches_scope(str(path), scope)
        return False

    def describe(self) -> str:
        entries = sorted(self._names)
        for tool_name, scopes in sorted(self._scopes.items()):
            entries.extend(f"{tool_name} “{s}”" for s in sorted(scopes))
        return ", ".join(entries) if entries else "(none)"
