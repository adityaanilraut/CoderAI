"""UI command handlers for the bridge (``_COMMAND_HANDLERS`` dispatch table).

Each handler takes ``(server, msg)`` where ``server`` is the
:class:`~coderAI.bridge.controller.UIBridge` and ``msg`` is the raw command
payload. Command names and their emitted events are pinned by
``tests/test_event_contract.py`` and documented in ``docs/CHAT_EVENTS.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.permissions import ApprovalRules
from coderAI.system.config import config_manager

from coderAI.bridge.serializers import (
    _agent_info_dict,
    _format_plan_message,
    _serialize_plan_for_ui,
)

if TYPE_CHECKING:
    from coderAI.bridge.controller import UIBridge
    from coderAI.core.agent_tracker import AgentTracker

logger = logging.getLogger(__name__)


def _tracker() -> "AgentTracker":
    # Resolved through the controller module at call time so tests patching
    # coderAI.bridge.controller.agent_tracker keep working (the handlers
    # lived in that module before the Phase 2b split).
    from coderAI.bridge import controller

    return controller.agent_tracker


# --- Persona / skills slash helpers ----------------------------------------


def _handle_persona_slash(server: "UIBridge", arg: str) -> None:
    """Inline ``/persona [name|default|list]`` handler.

    - ``/persona``           — list available personas (also when arg=="list")
    - ``/persona default``   — clear the active persona (also: ``none``, ``off``)
    - ``/persona <name>``    — switch to the named persona (filename stem)
    """
    from coderAI.core.agents import get_available_personas, resolve_persona_name

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_personas(project_root)

    name = (arg or "").strip().lower()
    if not name or name == "list":
        if not available:
            server.emit(
                "info",
                message=(
                    "No personas found in .coderAI/agents/. "
                    "Create <stem>.md files with YAML frontmatter to define one."
                ),
            )
            return
        current = server.agent.persona.name if server.agent.persona else "(default)"
        listing = "\n".join(f"  • {n}" for n in sorted(available))
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

    resolved = resolve_persona_name(arg, project_root)
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


# --- Command handlers -------------------------------------------------------


async def _cmd_send_message(server: UIBridge, msg: Dict[str, Any]) -> None:
    text = msg.get("text", "")
    async with server._turn_lock:
        server.tick_iteration()
        try:
            await server.agent.process_message(text)
        except Exception as e:
            logger.exception("process_message failed")
            server._emit_error(
                "internal",
                str(e),
                hint="See logs on stderr for the full traceback.",
            )
        finally:
            server.emit_status()
            server.emit_ready()


async def _cmd_cancel(server: UIBridge, msg: Dict[str, Any]) -> None:
    approvals_cancelled = server._cancel_pending_approvals("cancelled_by_user")
    agent_id = msg.get("agentId")
    if agent_id:
        ok = _tracker().cancel(agent_id)
        if ok:
            server.emit("info", message=f"Cancelled agent {agent_id[-8:]}")
        else:
            server.emit("warning", message=f"No active agent {agent_id}")
    else:
        active = _tracker().get_active()
        _tracker().cancel_all()
        suffix = f" and {approvals_cancelled} pending approval(s)" if approvals_cancelled else ""
        server.emit("info", message=f"Cancelled {len(active)} active agent(s){suffix}")


async def _cmd_set_model(server: UIBridge, msg: Dict[str, Any]) -> None:
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
    server.emit("session_patch", model=model, provider=server.agent.provider.__class__.__name__)
    # Verbose-only confirmation; the status bar carries the change in normal mode.
    server.emit("success", message=f"Switched model → {model}")


def _approval_rules(server: UIBridge) -> Optional[ApprovalRules]:
    rules = getattr(server.agent, "_tool_approval_allowlist", None)
    return rules if isinstance(rules, ApprovalRules) else None


async def _cmd_allow_tool(server: UIBridge, msg: Dict[str, Any]) -> None:
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


async def _cmd_disallow_tool(server: UIBridge, msg: Dict[str, Any]) -> None:
    tool = str(msg.get("tool", "")).strip()
    if not tool:
        server.emit("warning", message="Usage: /disallow-tool <tool-name>")
        return
    rules = _approval_rules(server)
    if rules is not None:
        rules.disallow(tool)
    server.emit("info", message=f"Tool approval memory removed for {tool}.")


async def _cmd_list_allowed_tools(server: UIBridge, _msg: Dict[str, Any]) -> None:
    rules = _approval_rules(server)
    names = rules.describe() if rules is not None else "(none)"
    server.emit("info", message=f"Always-allowed tools for this session: {names}")


async def _cmd_set_persona(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Switch the active persona programmatically (used by future UI picker).

    Payload: ``{"persona": "<name>"}``; empty/omitted/``"default"`` clears it.
    """
    raw = msg.get("persona") or (msg.get("payload") or {}).get("persona") or ""
    _handle_persona_slash(server, str(raw).strip())
    server.emit_ready()


async def _cmd_toggle_auto_approve(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.agent.auto_approve = not server.agent.auto_approve
    server.agent._configure_delegate_tool_context()
    # Status bar's safe/YOLO pill is the indicator in normal mode; the
    # success toast surfaces only in verbose.
    server.emit("session_patch", autoApprove=bool(server.agent.auto_approve))
    server.emit(
        "success",
        message=(
            "Auto-approve enabled (YOLO)" if server.agent.auto_approve else "Auto-approve disabled"
        ),
    )


async def _cmd_set_reasoning(server: UIBridge, msg: Dict[str, Any]) -> None:
    effort = str(msg.get("effort", "none")).lower()
    if effort not in ("high", "medium", "low", "none"):
        server.emit(
            "warning",
            message=f"Invalid reasoning effort: {effort!r}. Use high|medium|low|none.",
        )
        return
    # Persist in config so the next provider creation inherits the value.
    server.agent.config.reasoning_effort = effort
    # Also patch the live provider directly so the change takes effect on the
    # very next LLM call without requiring a model switch or restart.
    provider = getattr(server.agent, "provider", None)
    if provider is not None:
        try:
            setattr(provider, "reasoning_effort", effort)
        except Exception:
            # Provider may expose reasoning_effort as a read-only property;
            # the config value above still applies on the next provider build.
            logger.debug("could not patch live provider reasoning_effort", exc_info=True)
    server.emit("session_patch", reasoning=effort)
    # Status bar shows current reasoning level; no toast.


async def _cmd_tool_approval_resp(server: UIBridge, msg: Dict[str, Any]) -> None:
    tool_id = str(msg.get("toolId") or msg.get("id") or "")
    approve = bool(msg.get("approve", False))
    waiter = server._approval_waiters.pop(tool_id, None)
    if waiter is None:
        logger.warning("Late or invalid approval response for tool %s", tool_id)
        server.emit("warning", message="Tool approval response was received too late.")
        return
    # Calling ``set_result`` on an already-resolved or cancelled future would
    # raise ``InvalidStateError``. The waiter can complete out from under us
    # via timeout (``asyncio.wait_for``) or cancellation (``/clear``, ``/exit``)
    # before the UI's response arrives, so check first.
    if waiter.done():
        logger.warning(
            "Approval response for tool %s arrived after waiter resolved (state=%s); ignoring.",
            tool_id,
            "cancelled" if waiter.cancelled() else "done",
        )
        server.emit("warning", message="Tool approval response was received too late.")
        return
    waiter.set_result(approve)


async def _cmd_clear_context(server: UIBridge, msg: Dict[str, Any]) -> None:
    async with server._turn_lock:
        server.agent.session = None
        server.agent.context_manager.clear()
        server.agent.create_session()
    main_info = getattr(server.agent, "tracker_info", None)
    if main_info is not None:
        _tracker().clear_except({main_info.agent_id})
        main_info.status = AgentStatus.IDLE
        main_info.current_task = ""
        main_info.current_tool = None
        main_info.finished_at = None
        server.emit(
            "agent",
            phase="update",
            info=_agent_info_dict(main_info),
            parentId=main_info.parent_id,
        )
    else:
        _tracker().clear_except()
    server.emit("success", message="Session cleared")
    server.emit_status()


async def _cmd_rewind(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Rewind the conversation to before a prior user turn.

    Payload: ``{"turn": int, "files": bool}``. Truncates the session's message
    history back to that turn's checkpoint and, when ``files`` is set, reverts
    file edits made since then. The UI truncates its own timeline in parallel.
    """
    try:
        turn = int(msg.get("turn"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        server.emit("warning", message="Usage: /rewind <turn> [--files]")
        return
    restore_files = bool(msg.get("files", False))

    async with server._turn_lock:
        result = server.agent.rewind_to(turn, restore_files=restore_files)

    if not result.get("ok"):
        server.emit("warning", message=str(result.get("error", "Rewind failed.")))
        return

    parts = [f"Rewound to turn {result['turn']} ({result.get('label', '')})"]
    dropped = int(result.get("dropped_turns", 0) or 0)
    if dropped:
        parts.append(f"dropped {dropped} turn(s)")
    if restore_files:
        parts.append(f"restored {len(result.get('restored_files', []))} file(s)")
    server.emit("success", message=" — ".join(parts))

    file_errors = result.get("file_errors") or []
    if file_errors:
        server.emit(
            "warning",
            message="Some files could not be restored:\n" + "\n".join(file_errors),
        )
    server.emit_status()


async def _cmd_manage_context(server: UIBridge, msg: Dict[str, Any]) -> None:
    action = msg.get("action")
    path = msg.get("path")
    async with server._turn_lock:
        if action == "add":
            if not path:
                server.emit("warning", message="Path required to add to context.")
                return
            success = server.agent.context_manager.add_file(path)
            if success:
                server.emit("success", message=f"Added {path} to pinned context.")
            else:
                server.emit(
                    "warning",
                    message=f"Failed to add {path} to context (may be too large or invalid).",
                )
        elif action == "remove":
            if not path:
                server.emit("warning", message="Path required to remove from context.")
                return
            success = server.agent.context_manager.remove_file(path)
            if success:
                server.emit("success", message=f"Removed {path} from context.")
            else:
                server.emit("warning", message=f"Failed to remove {path} from context.")

        # Emit updated context state
        context_files = []
        pinned = server.agent.context_manager.pinned_files
        for path_str, content in pinned.items():
            context_files.append({"path": path_str, "size": len(content)})
        server.emit("context_state", files=context_files)
        server.emit_status()


async def _cmd_compact_context(server: UIBridge, msg: Dict[str, Any]) -> None:
    try:
        await server.agent.compact_context()
    except Exception as e:
        server._emit_error("internal", f"Compaction failed: {e}")
    else:
        server.emit("success", message="Context compacted")
    server.emit_status()


async def _cmd_get_state(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.emit_status()
    for info in _tracker().get_all():
        server.emit("agent", phase="update", info=_agent_info_dict(info), parentId=info.parent_id)

    context_files = []
    pinned = server.agent.context_manager.pinned_files
    for path_str, content in pinned.items():
        context_files.append({"path": path_str, "size": len(content)})
    server.emit("context_state", files=context_files)


async def _cmd_get_plan(server: UIBridge, msg: Dict[str, Any]) -> None:
    from coderAI.system.project_layout import read_current_plan

    pr = getattr(server.agent.config, "project_root", None) or "."
    plan = read_current_plan(str(pr))
    if not plan or not isinstance(plan, dict):
        server.emit(
            "info",
            message="No active execution plan. The agent can create one with the plan tool.",
        )
        return
    server.emit("plan_card", plan=_serialize_plan_for_ui(plan))
    server.emit("info", message=_format_plan_message(plan))


async def _cmd_get_tasks(server: UIBridge, _msg: Dict[str, Any]) -> None:
    server._emit_tasks_from_disk()


async def _cmd_list_personas(server: UIBridge, _msg: Dict[str, Any]) -> None:
    from coderAI.core.agents import get_available_personas

    project_root = getattr(server.agent.config, "project_root", ".")
    available = get_available_personas(project_root)
    current = server.agent.persona.name if server.agent.persona else None
    server.emit("available_personas", current=current, personas=sorted(available))


async def _cmd_list_skills(server: UIBridge, _msg: Dict[str, Any]) -> None:
    from ..tools.skills import get_available_skills

    project_root = getattr(server.agent.config, "project_root", ".")
    skills = get_available_skills(project_root)
    server.emit("available_skills", skills=skills)


async def _cmd_list_mcp_servers(server: UIBridge, _msg: Dict[str, Any]) -> None:
    """Emit the merged live + configured MCP server list for the /mcp picker."""
    from coderAI.tools.mcp import load_mcp_servers, mcp_client

    configured = load_mcp_servers().get("mcpServers", {})
    rows: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for name, info in mcp_client.servers.items():
        seen.add(name)
        rows.append(
            {
                "name": name,
                "connected": True,
                "disabled": bool(configured.get(name, {}).get("disabled")),
                "degraded": bool(info.get("degraded")),
                "tools": len(info.get("tools", [])),
                "transport": info.get("transport", "stdio"),
            }
        )
    for name, cfg in configured.items():
        if name in seen:
            continue
        rows.append(
            {
                "name": name,
                "connected": False,
                "disabled": bool(cfg.get("disabled")),
                "degraded": False,
                "tools": 0,
                "transport": cfg.get("transport", "stdio"),
            }
        )

    rows.sort(key=lambda r: str(r["name"]))
    server.emit("available_mcp_servers", servers=rows)
    if not rows:
        server.emit(
            "info",
            message="No MCP servers configured. Add one with `coderAI mcp add`.",
        )


async def _cmd_toggle_mcp_server(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Toggle an MCP server on/off — persistent (config) + live (connection).

    Off: disconnect the live connection now and mark it ``disabled`` so it does
    not auto-reconnect next session. On: connect now and clear the flag. The
    connect path mirrors ``ExecutionLoop._autoconnect_mcp_servers`` — the config
    was validated when the server was added, so no launcher re-check is needed.
    """
    from coderAI.tools.mcp import load_mcp_servers, mcp_client, set_mcp_server_disabled

    name = str(msg.get("server", "")).strip()
    if not name:
        server.emit("warning", message="Usage: /mcp <server-name>")
        return

    if name in mcp_client.servers:
        await mcp_client.disconnect(name)
        set_mcp_server_disabled(name, True)
        server.emit("success", message=f"MCP server '{name}' turned off (disconnected)")
        await _cmd_list_mcp_servers(server, {})
        return

    cfg = load_mcp_servers().get("mcpServers", {}).get(name)
    if not isinstance(cfg, dict):
        server.emit("warning", message=f"No MCP server named '{name}' is configured.")
        return

    transport = cfg.get("transport", "stdio")
    if transport == "sse":
        result = await mcp_client.connect_sse(name, cfg.get("url", ""))
    elif transport == "http":
        result = await mcp_client.connect_http(name, cfg.get("url", ""), cfg.get("headers"))
    else:
        result = await mcp_client.connect_stdio(name, cfg.get("command", ""), cfg.get("args"))

    if result.get("success"):
        set_mcp_server_disabled(name, False)
        count = result.get("tools_discovered", 0)
        server.emit("success", message=f"MCP server '{name}' turned on ({count} tools)")
    else:
        server.emit("warning", message=f"Failed to connect '{name}': {result.get('error')}")
    await _cmd_list_mcp_servers(server, {})


async def _cmd_search_codebase(server: UIBridge, msg: Dict[str, Any]) -> None:
    query = msg.get("query", "")
    if not query:
        return
    try:
        from ..embeddings.factory import create_embedding_provider
        from coderAI.context.code_indexer import CodeIndexer

        project_root = getattr(server.agent.config, "project_root", ".")
        config = config_manager.load()
        provider = create_embedding_provider(config)
        if provider is None:
            server.emit(
                "warning",
                message="No embedding provider available for code search. Set openai_api_key.",
            )
            return
        indexer = CodeIndexer(str(Path(project_root).resolve()), provider)
        results = await indexer.search(query=query, top_k=10)
        if not results:
            server.emit("info", message=f"No semantic search results found for '{query}'.")
            return
        out = [f"Semantic search results for '{query}':\n"]
        for r in results:
            snippet = r["text"].strip().split("\n")[0][:80]
            out.append(
                f"• {r['file_path']} L{r['start_line']}-{r['end_line']} (score: {r['score']:.2f})\n  {snippet}..."
            )
        server.emit("info", message="\n".join(out))
    except Exception as e:
        server.emit("warning", message=f"Codebase search failed: {e}")


async def _cmd_list_models(server: UIBridge, _msg: Dict[str, Any]) -> None:
    """Return all available models grouped by provider for the model-picker UI."""
    from ..llm.anthropic import MODEL_ALIASES
    from ..llm.deepseek import DeepSeekProvider
    from ..llm.groq import GroqProvider
    from ..llm.openai import OpenAIProvider
    from ..llm.gemini import GeminiProvider

    server.emit(
        "available_models",
        current=server.agent.model,
        models={
            "Anthropic": sorted(MODEL_ALIASES.keys()),
            "OpenAI": sorted(OpenAIProvider.SUPPORTED_MODELS.keys()),
            "DeepSeek": sorted(DeepSeekProvider.SUPPORTED_MODELS.keys()),
            "Groq": sorted(GroqProvider.SUPPORTED_MODELS.keys()),
            "Gemini": sorted(GeminiProvider.SUPPORTED_MODELS.keys()),
            "Local": ["lmstudio", "ollama"],
        },
    )


async def _cmd_reference(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Emit long-form help text (models, cost, system status, config, info, tasks)."""
    from coderAI.bridge.chat_reference import build_tasks_text, resolve_reference_text

    topic = str(msg.get("topic", "")).strip()
    if not topic:
        server.emit(
            "warning",
            message="Missing topic. Try /version, /models, /cost, /system, /config, /info, /tasks.",
        )
        return
    t = topic.lower()
    if t in ("tasks", "todos", "task"):
        pr = getattr(server.agent.config, "project_root", None) or "."
        try:
            text = await build_tasks_text(pr)
        except Exception as e:
            server.emit("warning", message=f"Tasks: {e}")
            return
        server.emit("info", message=text)
        return
    try:
        text = resolve_reference_text(t, server.agent)
    except ValueError as e:
        server.emit("warning", message=str(e))
        return
    except Exception as e:
        server.emit("warning", message=f"Reference failed: {e}")
        return
    server.emit("info", message=text)


async def _cmd_set_default_model(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Persist default_model in global config (like ``coderAI set-model``)."""
    from ..llm.factory import get_all_model_ids

    model_name = str(msg.get("model") or "").strip()
    if not model_name:
        server.emit("warning", message="Usage: /default <model>")
        return

    if model_name not in get_all_model_ids():
        server.emit(
            "warning",
            message=(
                f"Invalid model name: {model_name}. "
                "Use /models for groups; names must match provider IDs exactly."
            ),
        )
        return
    config_manager.set("default_model", model_name)
    current = server.agent.model
    if current != model_name:
        server.emit(
            "info",
            message=(
                f"Saved default model → {model_name}. "
                f"Current session is still using {current}; "
                f"use /model {model_name} to switch now."
            ),
        )
    else:
        server.emit(
            "info",
            message=f"Saved default model → {model_name} (already active).",
        )


async def _cmd_set_verbosity(server: UIBridge, msg: Dict[str, Any]) -> None:
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


async def _cmd_exit(server: UIBridge, msg: Dict[str, Any]) -> None:
    server.emit("goodbye", reason="user")
    server._said_goodbye = True
    server._exit.set()


async def _cmd_init_project(server: UIBridge, _msg: Dict[str, Any]) -> None:
    project_root = Path(getattr(server.agent.config, "project_root", ".")).resolve()
    dot_dir = project_root / ".coderAI"

    created_dirs: list[str] = []
    created_files: list[str] = []
    skipped_files: list[str] = []

    dirs_to_create = [
        dot_dir / "agents",
        dot_dir / "skills",
        dot_dir / "rules",
    ]

    for d in dirs_to_create:
        try:
            d.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(d.relative_to(project_root)))
        except OSError as e:
            server._emit_error("tool", f"Cannot create {d.name}: {e}")
            return

    files_to_create: list[tuple[Path, str]] = [
        (
            project_root / "CODERAI.md",
            "\n".join(
                [
                    "# Project Guidance for CoderAI",
                    "",
                    "Describe your project here so CoderAI can work effectively:",
                    "",
                    "## Project Overview",
                    "- What does this project do?",
                    "- What is the tech stack?",
                    "",
                    "## Key Conventions",
                    "- Code style preferences (e.g. tabs vs spaces, naming conventions)",
                    "- Testing framework and how to run tests",
                    "- Branch naming and PR workflow",
                    "",
                    "## Common Commands",
                    "- `npm run dev` / `make run` — start development server",
                    "- `npm test` / `pytest` — run tests",
                    "- `npm run lint` / `ruff check .` — lint code",
                    "",
                    "## Important Notes",
                    "- Any gotchas or context the AI should always remember",
                    "- Links to docs, design files, or relevant resources",
                    "",
                ]
            ),
        ),
        (
            dot_dir / "agents" / "planner.md",
            "\n".join(
                [
                    "---",
                    "name: planner",
                    "description: Planning specialist for complex features, refactors, and implementation sequencing.",
                    'tools: ["Read", "Grep", "Glob", "Bash", "Edit", "Write"]',
                    "model: sonnet",
                    "---",
                    "",
                    "You create implementation plans that are specific, incremental, and testable.",
                    "",
                    "## Workflow",
                    "",
                    "1. Read enough of the codebase to understand the real constraints.",
                    "2. Break the work into concrete steps with file paths when possible.",
                    "3. Call out dependencies, risks, and validation points.",
                    "4. Prefer plans that can be delivered in small, verifiable increments.",
                    "",
                    "## Output Expectations",
                    "",
                    "- Separate requirements, implementation steps, and risks.",
                    "- Include verification guidance.",
                    "- Avoid claiming any persona or workflow is activated automatically.",
                    "",
                ]
            ),
        ),
        (
            dot_dir / "rules" / "001-common-principles.md",
            "\n".join(
                [
                    "# 001: Common Principles",
                    "",
                    "This rule applies universally to all agents operating within this project. Follow these principles at all times:",
                    "",
                    "## 1. Test-Driven Development (TDD)",
                    "- **Always write tests first:** When implementing new features or fixing bugs, write a failing test before writing the implementation code.",
                    "- **Verify Coverage:** Ensure that all new core logic is covered by tests.",
                    "- **Independence:** Tests should not rely on shared state or external systems without proper mocking.",
                    "",
                    "## 2. Security First",
                    "- **No Hardcoded Secrets:** Never hardcode API keys, tokens, passwords, or connection strings in the source code. Use environment variables (e.g., `os.environ.get()`).",
                    "- **Input Validation:** Always validate and sanitize user input at the boundaries of the application.",
                    "- **Defense in Depth:** Do not assume that internal components are safe from malicious input.",
                    "",
                    "## 3. Tool Usage & Autonomy",
                    "- **Act Proactively:** Use your available tools (`Read`, `Grep`, `Bash`, etc.) to gather necessary context. Do not guess file paths or function names.",
                    "- **Verify Assumptions:** If you are unsure about how a component works, read the code or run a test script to understand its behavior before making changes.",
                    "",
                    "## 4. Communication",
                    "- **Clarity and Precision:** When reporting findings or documenting code, be concise but factually complete.",
                    "- **Cite Sources:** Reference specific file paths and line numbers when discussing code changes.",
                    "",
                    "## 5. Plan-First Workflow",
                    "- **Plan before you build:** For any task involving multiple steps, multiple file edits, or non-trivial implementation work, call the `plan` tool with `action='create'` before starting.",
                    "- **Track granular work:** Use `manage_tasks` (`add` / `start` / `complete`) alongside the plan to maintain a working checklist.",
                    "- **Skip planning only for trivial asks:** Single-file reads, greetings, one-line answers, and simple lookups do not need a plan.",
                    "",
                ]
            ),
        ),
        (
            dot_dir / "tasks.json",
            "[]\n",
        ),
    ]

    for filepath, content in files_to_create:
        rel = str(filepath.relative_to(project_root))
        if filepath.exists():
            skipped_files.append(rel)
            continue
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            created_files.append(rel)
        except OSError as e:
            server._emit_error("tool", f"Cannot write {rel}: {e}")
            return

    lines = [f"Scaffolded .coderai/ in {project_root.name}:"]
    if created_dirs:
        lines.append(f"  {len(created_dirs)} directories created")
    for f in created_files:
        lines.append(f"  created: {f}")
    for f in skipped_files:
        lines.append(f"  skipped (exists): {f}")

    server.emit("success", message="\n".join(lines))


async def _cmd_cancel_agent(server: UIBridge, msg: Dict[str, Any]) -> None:
    """Cancel a specific sub-agent by ID."""
    # ``/kill`` (coderAI/tui/slash.py) enqueues ``agentId`` at the top level via
    # ``enqueue_command("cancel_agent", agentId=...)``; older callers nested it
    # under ``payload``. Accept both so the TUI command actually reaches a target.
    agent_id = msg.get("agentId") or (msg.get("payload") or {}).get("agentId")
    if not agent_id:
        server.emit("error", category="protocol", message="cancel_agent requires agentId")
        return
    cancelled = _tracker().cancel(agent_id)
    server.emit(
        "success",
        message=f"Sub-agent {agent_id} cancellation {'requested' if cancelled else 'failed (not found)'}",
    )


async def _cmd_trust(server: UIBridge, msg: Dict[str, Any]) -> None:
    """``/trust`` — manage workspace trust for the current project root.

    Payload: ``{"action": "grant"|"revoke"|"status"}`` (default ``grant``).
    Trusting enables this repo's ``.coderAI`` hooks and ``config.json`` overlay;
    the ``config.json`` overlay applies on the next launch.
    """
    from coderAI.system.trust import workspace_trust

    action = str(msg.get("action") or (msg.get("payload") or {}).get("action") or "grant").strip()
    root = getattr(server.agent.config, "project_root", ".") or "."
    if action == "revoke":
        removed = workspace_trust.revoke_trust(root)
        server.emit(
            "info",
            message=(
                f"Workspace trust revoked for {root}."
                if removed
                else f"Workspace was not trusted: {root}"
            ),
        )
    elif action == "status":
        state = "trusted" if workspace_trust.is_trusted(root) else "untrusted"
        server.emit("info", message=f"Workspace {root} is {state}.")
    else:
        workspace_trust.record_trust(root)
        server.emit(
            "success",
            message=f"Workspace trusted: {root}. Project hooks are now enabled "
            "(config.json overlay applies on next launch).",
        )
    server.emit_status()


_COMMAND_HANDLERS: Dict[str, Callable[["UIBridge", Dict[str, Any]], Awaitable[None]]] = {
    "send_message": _cmd_send_message,
    "trust": _cmd_trust,
    "allow_tool": _cmd_allow_tool,
    "disallow_tool": _cmd_disallow_tool,
    "list_allowed_tools": _cmd_list_allowed_tools,
    "cancel": _cmd_cancel,
    "cancel_agent": _cmd_cancel_agent,
    "set_model": _cmd_set_model,
    "set_reasoning": _cmd_set_reasoning,
    "set_persona": _cmd_set_persona,
    "toggle_auto_approve": _cmd_toggle_auto_approve,
    "tool_approval_resp": _cmd_tool_approval_resp,
    "clear_context": _cmd_clear_context,
    "rewind": _cmd_rewind,
    "compact_context": _cmd_compact_context,
    "manage_context": _cmd_manage_context,
    "get_state": _cmd_get_state,
    "get_plan": _cmd_get_plan,
    "get_tasks": _cmd_get_tasks,
    "list_models": _cmd_list_models,
    "list_personas": _cmd_list_personas,
    "list_skills": _cmd_list_skills,
    "list_mcp_servers": _cmd_list_mcp_servers,
    "toggle_mcp_server": _cmd_toggle_mcp_server,
    "search_codebase": _cmd_search_codebase,
    "reference": _cmd_reference,
    "set_default_model": _cmd_set_default_model,
    "set_verbosity": _cmd_set_verbosity,
    "init_project": _cmd_init_project,
    "exit": _cmd_exit,
}
