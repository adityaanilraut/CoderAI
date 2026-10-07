"""Dynamic subagent spawning, descriptor validation, depth quotas, and scratchpad sandboxing."""

from __future__ import annotations

import logging
import os
import math
import pathlib
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_owned_scratchpads: dict[str, tuple[int, int]] = {}

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
        from coderai.subagents.registry import is_tool_allowed

        if self.deny and is_tool_allowed(tool_name, "allowlist", tuple(self.deny)):
            return False
        if self.allow is not None:
            return is_tool_allowed(tool_name, "allowlist", tuple(self.allow))
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
    if raw is None:
        return SubagentDescriptor()
    if not isinstance(raw, dict):
        raise ValueError("Descriptor must be an object")

    version = raw.get("version", SUBAGENT_DESCRIPTOR_VERSION)
    if version != SUBAGENT_DESCRIPTOR_VERSION or isinstance(version, bool):
        raise ValueError("Unsupported descriptor version")
    mode = str(raw.get("mode", "one-shot")).lower()
    if mode not in ("one-shot", "continuable"):
        raise ValueError("Descriptor mode must be one-shot or continuable")

    provider = str(raw.get("provider", "in_process"))
    if provider not in {"in_process", "acp", "claude_code", "codex"}:
        raise ValueError("Unsupported descriptor provider")
    label = str(raw.get("label", ""))
    agent_provider = raw.get("agentProvider") or raw.get("agent_provider")
    agent_model = raw.get("agentModel") or raw.get("agent_model")
    persona = raw.get("persona")

    tf_raw = raw.get("toolFilter", raw.get("tool_filter"))
    tool_filter: ToolRestriction | None = None
    if tf_raw is not None and not isinstance(tf_raw, dict):
        raise ValueError("toolFilter must be an object")
    if isinstance(tf_raw, dict):
        allow = tf_raw.get("allow")
        deny = tf_raw.get("deny")
        from coderai.subagents.registry import normalize_tool_list

        normalized_allow = normalize_tool_list(allow)
        normalized_deny = normalize_tool_list(deny)
        tool_filter = ToolRestriction(
            allow=list(normalized_allow) if normalized_allow is not None else None,
            deny=list(normalized_deny) if normalized_deny is not None else None,
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
    from coderai.utils.storage import owned_path, parent_directory, storage_id

    storage_id(session_id)
    storage_id(prefix)
    if len(prefix) > 32:
        raise ValueError("Scratchpad prefix is too long")
    root = pathlib.Path(project_root).resolve()
    scratch_root = owned_path(root, ".coderai", "scratch")
    try:
        directory = owned_path(scratch_root, f"{prefix}{session_id[:64]}_{uuid.uuid4().hex}")
        with parent_directory(directory, create=True) as parent:
            os.mkdir(directory if parent is None else directory.name, mode=0o700, dir_fd=parent)
    except OSError as exc:
        logger.warning(
            "Could not create project scratchpad in %s (%s). Falling back to temp directory.",
            scratch_root,
            exc,
        )
        directory = pathlib.Path(tempfile.mkdtemp(prefix=f"{prefix}{session_id}_"))
    directory = directory.resolve()
    info = directory.stat()
    _owned_scratchpads[str(directory)] = (info.st_dev, info.st_ino)
    return str(directory)


def cleanup_subagent_scratchpad(scratchpad_path: str | None) -> None:
    """Safely clean up or prune a temporary subagent scratchpad directory."""
    if not scratchpad_path:
        return
    try:
        p = pathlib.Path(scratchpad_path)
        identity = _owned_scratchpads.pop(str(p), None)
        if p.exists() and p.is_dir():
            from coderai.utils.storage import remove_path

            info = p.lstat()
            if not p.is_symlink() and identity == (info.st_dev, info.st_ino):
                remove_path(p, tree=True)
    except Exception as exc:
        logger.debug("Failed to clean up scratchpad %s: %s", scratchpad_path, exc)


MAX_SUBAGENT_ITERATIONS = DEFAULT_SUBAGENT_MAX_ITERATIONS
MAX_SUBAGENT_DEPTH = DEFAULT_MAX_SUBAGENT_DEPTH
DEFAULT_SUBAGENT_TIMEOUT = DEFAULT_SUBAGENT_TIMEOUT_SECONDS


@dataclass
class SubAgentSpec:
    """Specification for spawning an isolated sub-agent."""

    description: str
    prompt: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str | None = None  # "read_only" | "general" (defaults to read_only in __post_init__)
    provider: str = "in_process"  # "in_process" | "acp" | "claude_code" | "codex"
    timeout_seconds: float = DEFAULT_SUBAGENT_TIMEOUT
    max_iterations: int = MAX_SUBAGENT_ITERATIONS
    depth: int = 0
    max_depth: int = MAX_SUBAGENT_DEPTH
    token_budget: int | None = None
    max_tokens: int | None = None
    isolated_cwd: str | None = None
    dry_run: bool = False
    parent_session_id: str | None = None
    parent_tool_call_id: str | None = None
    allowed_tools: list[str] | None = None
    extra_context: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    root_agent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    handle: Any | None = None
    seed_messages: list[dict[str, Any]] | None = None
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
    on_process_start: Any | None = None
    on_process_exit: Any | None = None
    on_process_stdout: Any | None = None
    on_process_timeout_control: Any | None = None
    on_background_process_complete: Any | None = None
    session_manager: Any | None = None
    project_root: str | None = None
    checkpoint_store: Any | None = None

    def __post_init__(self) -> None:
        from coderai.subagents.registry import normalize_tool_list
        from coderai.utils.storage import storage_id

        if self.provider not in {"in_process", "acp", "claude_code", "codex"}:
            raise ValueError("Unsupported child provider")
        storage_id(self.task_id)
        if self.parent_session_id:
            storage_id(self.parent_session_id)
        if self.mode is not None and self.mode not in {"read_only", "general"}:
            raise ValueError("Invalid child permission mode")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Child timeout must be finite and positive")
        for label, value in (
            ("max_iterations", self.max_iterations),
            ("max_depth", self.max_depth),
            ("token_budget", self.token_budget),
            ("max_tokens", self.max_tokens),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"{label} must be a positive integer")
        if not isinstance(self.depth, int) or isinstance(self.depth, bool) or self.depth < 0:
            raise ValueError("Child depth must be a nonnegative integer")
        allowed = normalize_tool_list(self.allowed_tools)
        excluded = normalize_tool_list(self.exclude_tools)
        self.allowed_tools = list(allowed) if allowed is not None else None
        self.exclude_tools = list(excluded) if excluded is not None else None
        # Resolve type policy and role instructions from registry.
        # Deny-by-default: an unknown subagent_type yields an empty allowlist
        # (no tools permitted) rather than silently running unrestricted.
        if self.subagent_type:
            try:
                from coderai.subagents.registry import get_subagent_definition, resolve_tool_policy

                role_root = self.project_root or self.isolated_cwd
                defn = get_subagent_definition(self.subagent_type, project_root=role_root)
                if defn is None:
                    logger.warning(
                        "Unknown subagent_type '%s'; denying all tools by default.",
                        self.subagent_type,
                    )
                    self.allowed_tools = []
                    self.mode = "read_only"
                else:
                    policy, tools = resolve_tool_policy(
                        self.subagent_type, requested=self.allowed_tools, project_root=role_root
                    )
                    if policy == "allowlist":
                        self.allowed_tools = list(tools)

                    if self.model is None and getattr(defn, "model", None):
                        self.model = defn.model
                    if defn.exclude_tools:
                        self.exclude_tools = list(
                            dict.fromkeys((self.exclude_tools or []) + list(defn.exclude_tools))
                        )
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
                raise ValueError(f"Cannot resolve child policy for {self.subagent_type!r}") from exc

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

    cancellation = getattr(context, "cancellation_event", None)
    if isinstance(cancellation, threading.Event) and cancellation.is_set():
        raise ValueError("Cannot spawn from a cancelled invocation")
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
            raise ValueError("Invalid child permission mode")

    # Timeout resolution
    timeout_raw = args.get("timeout_seconds")
    if timeout_raw is not None:
        if isinstance(timeout_raw, bool):
            raise ValueError("Invalid child timeout")
        try:
            timeout_s = float(timeout_raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid child timeout") from exc
    else:
        timeout_s = defaults["timeout_seconds"]

    # Max depth resolution
    max_depth = defaults["max_depth"]

    def _parse_opt_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Child limits must be integers")
        try:
            return int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Child limits must be integers") from exc

    # Max iterations resolution
    max_iter_raw = args.get("max_iterations")
    if max_iter_raw is not None:
        max_iterations = _parse_opt_int(max_iter_raw)
    else:
        max_iterations = defaults["max_iterations"]

    # Lineage comes from the running parent's registry entry, never tool args.
    from coderai.subagents.core import get_agent_registry

    session_id = getattr(context, "session_id", None)
    parent_handle = (
        next((h for h in get_agent_registry().list() if h.run_session_id == session_id), None)
        if session_id
        else None
    )
    if (
        parent_handle is not None
        and parent_handle.spec is not None
        and parent_handle.spec.max_depth is not None
    ):
        max_depth = min(max_depth, parent_handle.spec.max_depth)

    # Depth resolution
    if depth is None:
        depth = parent_handle.depth + 1 if parent_handle is not None else 0

    # Numeric budgets
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
        "parent_tool_call_id": (getattr(context, "tool_call", None) or {}).get("id"),
        "extra_context": extra_context,
        "seed_messages": seed_messages,
        "sandbox_mode": getattr(context, "sandbox_mode", None),
        "plan_mode": getattr(context, "plan_mode", False),
        "on_before_file_mutation": getattr(context, "on_before_file_mutation", None),
        "on_after_file_mutation": getattr(context, "on_after_file_mutation", None),
        "on_process_start": getattr(context, "on_process_start", None),
        "on_process_exit": getattr(context, "on_process_exit", None),
        "on_process_stdout": getattr(context, "on_process_stdout", None),
        "on_process_timeout_control": getattr(context, "on_process_timeout_control", None),
        "on_background_process_complete": getattr(context, "on_background_process_complete", None),
        "session_manager": getattr(context, "session_manager", None),
        "project_root": getattr(context, "project_root", None),
        "isolated_cwd": getattr(context, "isolated_cwd", None),
        "dry_run": getattr(context, "dry_run", False),
        "parent_agent_id": parent_handle.id if parent_handle is not None else None,
        "root_agent_id": (
            parent_handle.root_agent_id or parent_handle.id if parent_handle is not None else None
        ),
    }
    spec_kwargs.update(overrides)
    # Internal callers may narrow the quota, but cannot widen the configured
    # or inherited parent limit while constructing a child.
    requested_max_depth = spec_kwargs.get("max_depth")
    spec_kwargs["max_depth"] = (
        min(max_depth, requested_max_depth) if requested_max_depth is not None else max_depth
    )
    spec = SubAgentSpec(**spec_kwargs)
    from coderai.subagents.registry import intersect_tool_lists

    parent_tools = getattr(context, "allowed_tools", None)
    if parent_tools is not None:
        allowed = intersect_tool_lists(spec.allowed_tools, parent_tools)
        spec.allowed_tools = list(allowed) if allowed is not None else None
    if parent_handle is not None and parent_handle.spec is not None:
        parent_spec = parent_handle.spec
        if parent_spec.mode == "read_only":
            spec.mode = "read_only"
        allowed = intersect_tool_lists(spec.allowed_tools, parent_spec.allowed_tools)
        parent_exclusions = list(parent_spec.exclude_tools or [])
        if parent_spec.descriptor is not None and parent_spec.descriptor.tool_filter is not None:
            restriction = parent_spec.descriptor.tool_filter
            allowed = intersect_tool_lists(allowed, restriction.allow)
            parent_exclusions.extend(restriction.deny or [])
        spec.allowed_tools = list(allowed) if allowed is not None else None
        spec.exclude_tools = (
            list(dict.fromkeys((spec.exclude_tools or []) + parent_exclusions)) or None
        )
    return spec
