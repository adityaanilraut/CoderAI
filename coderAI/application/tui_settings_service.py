"""Live agent settings, approval memory, persona, skill, and trust service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from coderAI.core.permissions import ApprovalRules

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge

logger = logging.getLogger(__name__)


def _handle_persona_slash(server: "UIBridge", arg: str) -> None:
    """Inline ``/persona [name|default|list]`` handler.

    - ``/persona``           — list available personas (also when arg=="list")
    - ``/persona default``   — clear the active persona (also: ``none``, ``off``)
    - ``/persona <name>``    — switch to the named persona (filename stem)
    """
    from coderAI.core.personas import (
        get_available_persona_descriptors,
        resolve_persona_name,
    )

    project_root = getattr(server.agent.config, "project_root", ".")
    workspace_trusted = bool(getattr(server.agent, "_workspace_trusted", False))
    descriptors = get_available_persona_descriptors(
        project_root,
        include_project=workspace_trusted,
    )
    available = [item.name for item in descriptors]

    name = (arg or "").strip().lower()
    if not name or name == "list":
        if not available:
            server.emit("info", message="No built-in, user, or project personas were found.")
            return
        current = server.agent.persona.name if server.agent.persona else "(default)"
        listing = "\n".join(
            f"  • {item.name} [{item.scope}]"
            for item in sorted(descriptors, key=lambda entry: entry.name)
        )
        server.emit(
            "info",
            message=f"Available personas (current: {current}):\n{listing}\n\nUse /persona <name> to switch · /persona default to clear.",
        )
        return

    if name in ("default", "none", "off", "clear"):
        server.agent.set_persona(None)
        server.emit("session_patch", model=server.agent.model, persona=None)
        server.emit("info", message="Persona cleared — back to the default agent.")
        return

    resolved = resolve_persona_name(
        arg,
        project_root,
        include_project=workspace_trusted,
    )
    if not resolved:
        hint = f"Persona '{arg}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
        server.emit("warning", message=hint)
        return

    applied = server.agent.set_persona(resolved)
    if applied is None:
        server.emit("warning", message=f"Failed to apply persona '{resolved}'.")
        return
    # Persona may carry a model override that ``set_persona`` activated; surface
    # the patch so the UI status bar refreshes.
    server.emit(
        "session_patch",
        model=server.agent.model,
        provider=server.agent.provider.__class__.__name__,
        persona=applied.name,
    )
    server.emit("info", message=f"Persona switched → {applied.name}")


def _approval_rules(server: UIBridge) -> Optional[ApprovalRules]:
    rules = getattr(server.agent, "_tool_approval_allowlist", None)
    return rules if isinstance(rules, ApprovalRules) else None


async def _cmd_set_model(server: UIBridge, msg: dict[str, Any]) -> None:
    model = msg.get("model", "")
    old_model = server.agent.model
    old_provider = server.agent.provider
    server.agent.model = model
    try:
        server.agent._replace_provider()
    except Exception as e:
        server.agent.model = old_model
        server.agent.provider = old_provider
        context_controller = getattr(server.agent, "context_controller", None)
        if context_controller is not None:
            context_controller.provider = old_provider
        # Re-wire the delegate tool context to the restored provider so a failed
        # switch doesn't leave it pointing at a dead/half-built one (mirrors the
        # success path below).
        try:
            server.agent._configure_delegate_tool_context()
        except Exception:
            pass
        server._emit_error("provider", f"Could not switch to {model}: {e}")
        return
    # No usage re-sync: the Agent owns the running token totals and the loop
    # attributes each call's usage from the response, so the freshly created
    # provider's zeroed counters don't perturb session accounting.
    server.agent._configure_delegate_tool_context()
    # Persist the hot-switch on the active session so replays from
    # ``~/.coderAI/history/`` report the model that was actually used for
    # each turn from this point forward.
    if server.agent.session is not None:
        server.agent.session.model = model
    server.agent._refresh_session_system_prompt()
    server.emit("session_patch", model=model, provider=server.agent.provider.__class__.__name__)
    # Verbose-only confirmation; the status bar carries the change in normal mode.
    server.emit("success", message=f"Switched model → {model}")


async def _cmd_allow_tool(server: UIBridge, msg: dict[str, Any]) -> None:
    tool = str(msg.get("tool", "")).strip()
    scope = str(msg.get("scope", "")).strip()
    if not tool:
        server.emit("warning", message="Usage: /allow-tool <tool-name> [command-prefix | path]")
        return
    rules = _approval_rules(server)
    if rules is None:
        server.emit("warning", message="Approval rules are unavailable in this session.")
        return
    accepted, message = rules.allow(tool, scope or None)
    server.emit("info" if accepted else "warning", message=message)


async def _cmd_disallow_tool(server: UIBridge, msg: dict[str, Any]) -> None:
    tool = str(msg.get("tool", "")).strip()
    if not tool:
        server.emit("warning", message="Usage: /disallow-tool <tool-name>")
        return
    rules = _approval_rules(server)
    if rules is not None:
        rules.disallow(tool)
    server.emit("info", message=f"Tool approval memory removed for {tool}.")


async def _cmd_list_allowed_tools(server: UIBridge, _msg: dict[str, Any]) -> None:
    rules = _approval_rules(server)
    names = rules.describe() if rules is not None else "(none)"
    server.emit("info", message=f"Always-allowed tools for this session: {names}")


async def _cmd_set_persona(server: UIBridge, msg: dict[str, Any]) -> None:
    """Switch the active persona programmatically (used by future UI picker).

    Payload: ``{"persona": "<name>"}``; empty/omitted/``"default"`` clears it.
    """
    raw = msg.get("persona") or (msg.get("payload") or {}).get("persona") or ""
    _handle_persona_slash(server, str(raw).strip())
    server.emit_ready()


async def _cmd_toggle_auto_approve(server: UIBridge, msg: dict[str, Any]) -> None:
    server.agent.auto_approve = not server.agent.auto_approve
    refresh_policy = getattr(server.agent, "_refresh_run_permission_policy", None)
    if callable(refresh_policy):
        refresh_policy()
    server.agent._configure_delegate_tool_context()
    server.agent._refresh_session_system_prompt()
    # Status bar's safe/YOLO pill is the indicator in normal mode; the
    # success toast surfaces only in verbose.
    server.emit("session_patch", autoApprove=bool(server.agent.auto_approve))
    server.emit(
        "success",
        message=(
            "Auto-approve enabled (YOLO)" if server.agent.auto_approve else "Auto-approve disabled"
        ),
    )


async def _cmd_set_auto_approve(server: UIBridge, msg: dict[str, Any]) -> None:
    """Idempotently enable or disable YOLO (``auto_approve``).

    Used by the approval modal's Always (a) path so a stale UI flag can never
    flip YOLO *off*. ``/yolo`` still uses the toggle command.
    """
    enabled = bool(msg.get("enabled", True))
    changed = bool(server.agent.auto_approve) != enabled
    server.agent.auto_approve = enabled
    if changed:
        refresh_policy = getattr(server.agent, "_refresh_run_permission_policy", None)
        if callable(refresh_policy):
            refresh_policy()
        server.agent._configure_delegate_tool_context()
        server.agent._refresh_session_system_prompt()
    server.emit("session_patch", autoApprove=enabled)
    if changed:
        server.emit(
            "success",
            message=("Auto-approve enabled (YOLO)" if enabled else "Auto-approve disabled"),
        )


async def _cmd_set_reasoning(server: UIBridge, msg: dict[str, Any]) -> None:
    effort = str(msg.get("effort", "none")).lower()
    if effort not in ("high", "medium", "low", "none"):
        server.emit(
            "warning", message=f"Invalid reasoning effort: {effort!r}. Use high|medium|low|none."
        )
        return
    from coderAI.llm.registry import get_spec

    model = getattr(server.agent, "model", "")
    spec = get_spec(model) if model else None
    if spec and not spec.supports_reasoning and effort != "none":
        server.emit(
            "warning",
            message=f"Model {model} does not support reasoning (only 'none' allowed). Tier: {spec.tier}.",
        )
        return
    server.agent.config.reasoning_effort = effort
    provider = getattr(server.agent, "provider", None)
    if provider is not None:
        try:
            provider.reasoning_effort = effort
        except Exception:
            logger.debug("could not patch live provider reasoning_effort", exc_info=True)
    server.emit("session_patch", reasoning=effort)


async def _cmd_list_personas(server: UIBridge, _msg: dict[str, Any]) -> None:
    from coderAI.core.personas import get_available_persona_descriptors

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_persona_descriptors(
        project_root,
        include_project=bool(getattr(server.agent, "_workspace_trusted", False)),
    )
    current = server.agent.persona.name if server.agent.persona else None
    server.emit(
        "available_personas",
        current=current,
        personas=sorted(item.name for item in available),
        personaScopes={item.name: item.scope for item in available},
    )


async def _cmd_list_skills(server: UIBridge, _msg: dict[str, Any]) -> None:
    """Emit skills for the /skills picker.

    Mirrors SkillManager: user skills (``~/.coderAI/skills``) are always listed;
    project skills (``.coderAI/skills``) require a trusted workspace snapshot.
    """
    from coderAI.skills.skill_manager import discover_local_skills

    project_root = getattr(server.agent.config, "project_root", ".")
    trusted = bool(getattr(server.agent, "_workspace_trusted", False))
    found = discover_local_skills(
        project_root,
        include_project=trusted,
        include_user=True,
    )
    skills = [{"name": s.name, "description": s.description, "source": s.source} for s in found]
    server.emit("available_skills", skills=skills)
    if not skills and not trusted:
        server.emit(
            "info",
            message=(
                "Project skills are disabled until this workspace is trusted "
                "(then restart CoderAI). User skills in ~/.coderAI/skills still "
                "appear here. Install with: coderAI skills install … --scope user"
            ),
        )


async def _cmd_set_verbosity(server: UIBridge, msg: dict[str, Any]) -> None:
    """Adjust the IPC server's event filter.

    Levels (least → most chatty):
      - quiet:   drop info/warning/success state toasts entirely.
      - normal:  drop success toasts only (default).
      - verbose: pass through everything including agent_status narration.
    """
    level = str(msg.get("level", "normal")).lower()
    if level not in ("quiet", "normal", "verbose"):
        server.emit(
            "warning",
            message=f"Invalid verbosity: {level!r}. Use quiet|normal|verbose.",
        )
        return
    server._verbosity = level
    # Echo the authoritative level back so the reducer's session.verbose stays
    # in sync with the tri-state _verbosity (the app's optimistic flip on
    # Ctrl-V is idempotent on this echo). Reuses the existing session_patch
    # contract event — no new event.
    server.emit("session_patch", verbosity=level)


async def _cmd_trust(server: UIBridge, msg: dict[str, Any]) -> None:
    """``/trust`` — manage workspace trust for the current project root.

    Payload: ``{"action": "grant"|"revoke"|"status"}`` (default ``grant``).
    Trust changes are persisted for the next Agent launch. They never alter the
    project-controlled surfaces active in the current session.
    """
    from coderAI.system.trust import workspace_trust

    action = str(msg.get("action") or (msg.get("payload") or {}).get("action") or "grant").strip()
    root = getattr(server.agent.config, "project_root", ".") or "."
    if action == "revoke":
        removed = workspace_trust.revoke_trust(root)
        active = bool(getattr(server.agent, "_workspace_trusted", False))
        server.emit(
            "info",
            message=(
                f"Workspace trust revoked for {root}. Restart CoderAI to disable project "
                "config, hooks, rules, skills, and personas."
                if removed and active
                else f"Workspace trust revoked for {root}."
                if removed
                else f"Workspace was not trusted: {root}"
            ),
        )
    elif action == "status":
        recorded = workspace_trust.is_trusted(root)
        active = bool(getattr(server.agent, "_workspace_trusted", False))
        if recorded == active:
            state = "trusted and active" if active else "untrusted"
            message = f"Workspace {root} is {state} for this session."
        elif recorded:
            message = f"Workspace trust is recorded for {root}; restart CoderAI to activate it."
        else:
            message = (
                f"Workspace trust is revoked for {root}; project surfaces remain active "
                "until CoderAI restarts."
            )
        server.emit("info", message=message)
    else:
        try:
            workspace_trust.record_trust(root)
        except (OSError, ValueError) as e:
            server.emit("warning", message=f"Workspace trust was not recorded: {e}")
            server.emit_status()
            return
        server.emit(
            "success",
            message=(
                f"Workspace trust recorded: {root}. Restart CoderAI to enable project "
                "config, hooks, rules, skills, and personas; they remain disabled in "
                "this session."
            ),
        )
    server.emit_status()
