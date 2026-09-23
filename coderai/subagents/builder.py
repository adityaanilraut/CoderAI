"""Dynamic subagent spawning, descriptor validation, depth quotas, and scratchpad sandboxing."""

from __future__ import annotations

import logging
import pathlib
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SUBAGENT_DESCRIPTOR_VERSION = 1
DEFAULT_MAX_SUBAGENT_DEPTH = 3
DEFAULT_SUBAGENT_TIMEOUT_SECONDS = 90.0
DEFAULT_SUBAGENT_MAX_ITERATIONS = 20


@dataclass
class ToolRestriction:
    """Explicit tool allow/deny whitelist/blacklist for subagent sandboxing."""

    allow: list[str] | None = None
    deny: list[str] | None = None

    def is_tool_permitted(self, tool_name: str) -> bool:
        if self.deny and tool_name in self.deny:
            return False
        if self.allow is not None:
            return tool_name in self.allow
        return True

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        if self.allow is not None:
            res["allow"] = list(self.allow)
        if self.deny is not None:
            res["deny"] = list(self.deny)
        return res


@dataclass
class SubagentDescriptor:
    """Durable versioned descriptor declaring child agent execution modality."""

    version: int = SUBAGENT_DESCRIPTOR_VERSION
    mode: str = "one-shot"  # "one-shot" | "continuable"
    provider: str = "in_process"  # "in_process" | "acp" | "claude_code" | "codex"
    label: str = ""
    agent_provider: str | None = None
    agent_model: str | None = None
    persona: str | None = None
    tool_filter: ToolRestriction | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "mode": self.mode,
            "provider": self.provider,
            "label": self.label,
        }
        if self.agent_provider is not None:
            data["agentProvider"] = self.agent_provider
        if self.agent_model is not None:
            data["agentModel"] = self.agent_model
        if self.persona is not None:
            data["persona"] = self.persona
        if self.tool_filter is not None:
            data["toolFilter"] = self.tool_filter.to_dict()
        if self.extra_metadata:
            data["extraMetadata"] = self.extra_metadata
        return data


def parse_subagent_descriptor(raw: dict[str, Any] | None) -> SubagentDescriptor:
    """Validate and parse a raw dictionary into a SubagentDescriptor."""
    if not raw or not isinstance(raw, dict):
        return SubagentDescriptor()

    version = raw.get("version", SUBAGENT_DESCRIPTOR_VERSION)
    mode = str(raw.get("mode", "one-shot")).lower()
    if mode not in ("one-shot", "continuable", "read_only", "general"):
        mode = "one-shot"

    provider = str(raw.get("provider", "in_process"))
    label = str(raw.get("label", ""))
    agent_provider = raw.get("agentProvider") or raw.get("agent_provider")
    agent_model = raw.get("agentModel") or raw.get("agent_model")
    persona = raw.get("persona")

    tf_raw = raw.get("toolFilter") or raw.get("tool_filter")
    tool_filter: ToolRestriction | None = None
    if isinstance(tf_raw, dict):
        allow = tf_raw.get("allow")
        deny = tf_raw.get("deny")
        tool_filter = ToolRestriction(
            allow=list(allow) if isinstance(allow, list) else None,
            deny=list(deny) if isinstance(deny, list) else None,
        )

    return SubagentDescriptor(
        version=int(version) if isinstance(version, (int, float)) else SUBAGENT_DESCRIPTOR_VERSION,
        mode=mode,
        provider=provider,
        label=label,
        agent_provider=str(agent_provider) if agent_provider else None,
        agent_model=str(agent_model) if agent_model else None,
        persona=str(persona) if persona else None,
        tool_filter=tool_filter,
    )


@dataclass
class SubagentQuotaConfig:
    """Granular execution budgets and depth quotas for subagent spawning."""

    max_depth: int = DEFAULT_MAX_SUBAGENT_DEPTH
    max_tokens: int | None = None
    max_turns: int = DEFAULT_SUBAGENT_MAX_ITERATIONS
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT_SECONDS
    allow_nested_spawn: bool = False


def check_subagent_depth_quota(
    current_depth: int,
    max_depth: int = DEFAULT_MAX_SUBAGENT_DEPTH,
) -> tuple[bool, str | None]:
    """Verify if spawning a child agent at current_depth is permitted under max_depth quota."""
    if current_depth >= max_depth:
        return False, (
            f"RecursionLimitError: Sub-agent spawning denied. Current depth {current_depth} "
            f"exceeds max_depth (nesting depth quota of {max_depth})."
        )
    return True, None


def setup_subagent_scratchpad(
    project_root: str,
    session_id: str,
    prefix: str = "subagent_scratch_",
) -> str:
    """Create an isolated, dedicated scratchpad workspace directory for the subagent."""
    coderai_dir = pathlib.Path(project_root) / ".coderai" / "scratch" / session_id
    try:
        coderai_dir.mkdir(parents=True, exist_ok=True)
        return str(coderai_dir.resolve())
    except Exception as exc:
        logger.warning(
            "Could not create project scratchpad in %s (%s). Falling back to temp directory.",
            coderai_dir,
            exc,
        )
        temp_dir = tempfile.mkdtemp(prefix=f"{prefix}{session_id[:8]}_")
        return str(pathlib.Path(temp_dir).resolve())


def cleanup_subagent_scratchpad(scratchpad_path: str | None) -> None:
    """Safely clean up or prune a temporary subagent scratchpad directory."""
    if not scratchpad_path:
        return
    try:
        p = pathlib.Path(scratchpad_path)
        if p.exists() and p.is_dir():
            # If in temp directory, remove completely; if in .coderai/scratch, prune if empty
            if "tmp" in str(p) or "temp" in str(p):
                shutil.rmtree(p, ignore_errors=True)
    except Exception as exc:
        logger.debug("Failed to clean up scratchpad %s: %s", scratchpad_path, exc)


# --- SubAgentSpec (from coderai/core/subagent.py) ---
MAX_SUBAGENT_ITERATIONS = DEFAULT_SUBAGENT_MAX_ITERATIONS
MAX_SUBAGENT_DEPTH = DEFAULT_MAX_SUBAGENT_DEPTH
DEFAULT_SUBAGENT_TIMEOUT = DEFAULT_SUBAGENT_TIMEOUT_SECONDS


@dataclass
class SubAgentSpec:
    """Specification for spawning an isolated sub-agent."""

    description: str
    prompt: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    mode: str | None = None  # "read_only" | "general" (defaults to read_only in __post_init__)
    provider: str = "in_process"  # "in_process" | "acp" | "claude_code" | "codex"
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT
    max_iterations: int = MAX_SUBAGENT_ITERATIONS
    depth: int = 0
    max_depth: int = MAX_SUBAGENT_DEPTH
    token_budget: int | None = None
    max_tokens: int | None = None
    isolated_cwd: str | None = None
    scratchpad_dir: str | None = None
    dry_run: bool = False
    parent_session_id: str | None = None
    allowed_tools: list[str] | None = None
    extra_context: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    handle: Any | None = None
    seed_messages: list[dict[str, Any]] | None = None
    seed_events: list[Any] | None = None
    descriptor: SubagentDescriptor | None = None
    continuable: bool = False  # parked worker loop; survives multiple turns via its handle inbox
    subagent_type: str | None = (
        None  # Builtin or custom role (coder|explore|plan|architect|code-reviewer...)
    )
    system_prompt: str | None = None
    model: str | None = None
    exclude_tools: list[str] | None = None
    when_to_use: str | None = None
    sandbox_mode: str | None = None
    plan_mode: bool = False
    on_before_file_mutation: Any | None = None
    on_after_file_mutation: Any | None = None
    session_manager: Any | None = None

    def __post_init__(self) -> None:
        # Resolve type policy and role instructions from registry.
        # Deny-by-default: an unknown subagent_type yields an empty allowlist
        # (no tools permitted) rather than silently running unrestricted.
        if self.subagent_type:
            try:
                from coderai.subagents.registry import get_subagent_definition, resolve_tool_policy

                defn = get_subagent_definition(self.subagent_type, project_root=self.isolated_cwd)
                if defn is None:
                    logger.warning(
                        "Unknown subagent_type '%s'; denying all tools by default.",
                        self.subagent_type,
                    )
                    self.allowed_tools = []
                    self.mode = "read_only"
                else:
                    if self.allowed_tools is None:
                        mode, tools = resolve_tool_policy(
                            self.subagent_type, project_root=self.isolated_cwd
                        )
                        if mode == "allowlist":
                            self.allowed_tools = list(tools)

                    if self.model is None and getattr(defn, "model", None):
                        self.model = defn.model
                    if self.exclude_tools is None and getattr(defn, "exclude_tools", None):
                        self.exclude_tools = list(defn.exclude_tools)
                    if self.when_to_use is None and getattr(defn, "when_to_use", None):
                        self.when_to_use = defn.when_to_use

                    if self.mode is None:
                        self.mode = defn.mode or "read_only"
                    elif self.mode == "read_only":
                        pass  # explicit read_only must never be widened by role
                    elif self.mode == "general" and defn.mode == "read_only":
                        self.mode = "read_only"

                    if defn.system_prompt and not self.system_prompt:
                        self.system_prompt = defn.system_prompt
            except Exception as exc:
                logger.warning(
                    "Error resolving subagent policy for '%s': %s", self.subagent_type, exc
                )

        if self.mode is None:
            self.mode = "read_only"


def build_spec(
    context: Any,
    args: dict[str, Any] | None = None,
    *,
    description: str | None = None,
    prompt: str | None = None,
    mode: str | None = None,
    subagent_type: str | None = None,
    continuable: bool = False,
    depth: int | None = None,
    seed_messages: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> SubAgentSpec:
    """Build a SubAgentSpec using orchestration.resolve_subagent_defaults.

    Unifies spec construction across all spawn paths (subagent, subagent_fork,
    Task, and spawn_teammate) so that CODERAI_MAX_SUBAGENT_DEPTH,
    CODERAI_SUBAGENT_TIMEOUT_SECONDS, and CODERAI_SUBAGENT_MAX_ITERATIONS take effect.
    """
    from coderai.orchestration import resolve_subagent_defaults

    args = args or {}
    settings = None
    if hasattr(context, "session_manager") and context.session_manager:
        settings = getattr(context.session_manager, "get_resolved_settings", lambda: None)()
    elif hasattr(context, "settings") and isinstance(context.settings, dict):
        settings = context.settings
    defaults = resolve_subagent_defaults(settings)

    desc = (description or args.get("description", "") or "").strip()
    pr = (prompt or args.get("prompt", "") or "").strip()

    # Mode resolution
    resolved_mode = mode or args.get("mode")
    if resolved_mode is not None:
        resolved_mode = str(resolved_mode).strip().lower()
        if resolved_mode not in ("read_only", "general"):
            resolved_mode = "read_only"

    # Timeout resolution
    timeout_raw = args.get("timeout_seconds")
    if timeout_raw is not None:
        try:
            timeout_s = float(timeout_raw)
        except (ValueError, TypeError):
            timeout_s = defaults["timeout_seconds"]
    else:
        timeout_s = defaults["timeout_seconds"]

    # Max depth resolution
    max_depth = defaults["max_depth"]

    # Max iterations resolution
    max_iter_raw = args.get("max_iterations")
    if max_iter_raw is not None:
        try:
            max_iterations = int(max_iter_raw)
        except (ValueError, TypeError):
            max_iterations = defaults["max_iterations"]
    else:
        max_iterations = defaults["max_iterations"]

    # Depth resolution
    if depth is None:
        from coderai.subagents.core import get_agent_registry

        d = 0
        session_id = getattr(context, "session_id", None)
        if session_id:
            for handle in get_agent_registry().list():
                if getattr(handle, "run_session_id", None) == session_id:
                    d = handle.depth + 1
                    break
        depth = d

    # Numeric budgets
    def _parse_opt_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    token_budget = _parse_opt_int(args.get("token_budget"))
    max_tokens = _parse_opt_int(args.get("max_tokens"))
    resolved_type = (subagent_type or args.get("subagent_type") or "").strip().lower() or None

    raw_ctx = args.get("context")
    extra_context = raw_ctx.strip() if isinstance(raw_ctx, str) and raw_ctx.strip() else None

    spec_kwargs: dict[str, Any] = {
        "description": desc,
        "prompt": pr,
        "mode": resolved_mode,
        "subagent_type": resolved_type,
        "depth": depth,
        "max_depth": max_depth,
        "timeout_seconds": timeout_s,
        "max_iterations": max_iterations,
        "token_budget": token_budget,
        "max_tokens": max_tokens,
        "continuable": continuable,
        "parent_session_id": getattr(context, "session_id", None),
        "extra_context": extra_context,
        "seed_messages": seed_messages,
        "sandbox_mode": getattr(context, "sandbox_mode", None),
        "plan_mode": getattr(context, "plan_mode", False),
        "on_before_file_mutation": getattr(context, "on_before_file_mutation", None),
        "on_after_file_mutation": getattr(context, "on_after_file_mutation", None),
        "session_manager": getattr(context, "session_manager", None),
    }
    spec_kwargs.update(overrides)
    return SubAgentSpec(**spec_kwargs)
