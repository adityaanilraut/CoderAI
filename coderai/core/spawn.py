"""Dynamic subagent spawning, descriptor validation, depth quotas, and scratchpad sandboxing."""

from __future__ import annotations

import logging
import pathlib
import shutil
import tempfile
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
