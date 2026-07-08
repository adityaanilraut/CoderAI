"""Client-side slash command routing — registry-driven dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from coderAI.system.history import checkpoint_label
from coderAI.tui.export import timeline_to_markdown

if TYPE_CHECKING:
    from .controller import UIBridge
    from coderAI.tui.listeners import EventReducer


# ── context object passed to every handler ────────────────────────────


@dataclass
class SlashContext:
    controller: "UIBridge"
    reducer: "EventReducer"
    show_palette: Callable[[Optional[str]], None]
    show_search: Callable[[], None]
    show_context: Callable[[], None]
    clear_context: Callable[[], None]
    toggle_verbose: Callable[[], None]
    reveal_reasoning: Callable[[], None]
    confirm_exit: Callable[[], bool]
    set_search_filter: Callable[[str], None]
    retry_agent: Callable[[], None]
    rewind_timeline: Callable[[int], None]
    resume_session: Callable[[Optional[str]], None]

    def toast(self, level: str, message: str) -> None:
        self.reducer.toast(level, message)


# ── individual command handlers ────────────────────────────────────────


def _cmd_help(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.show_palette(None)
    return True


def _cmd_clear(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.clear_context()
    return True


def _cmd_compact(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("compact_context")
    ctx.toast("info", "compacting context…")
    return True


def _cmd_model(ctx: SlashContext, arg: str, head: str) -> bool:
    sub = arg.split() if arg else []
    if not sub:
        ctx.controller.enqueue_command("list_models")
        ctx.show_palette("models")
        return True
    if sub[0].lower() == "default":
        target = " ".join(sub[1:]).strip()
        if not target:
            ctx.toast("warning", "Usage: /model default <name>")
            return True
        ctx.controller.enqueue_command("set_default_model", model=target)
        ctx.toast("success", f"Default model set to {target}")
        return True
    ctx.controller.enqueue_command("set_model", model=arg)
    ctx.toast("success", f"Model set to {arg}")
    return True


def _cmd_reasoning(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.show_palette("reasoning")
        return True
    norm = arg.lower()
    if norm not in ("high", "medium", "low", "none"):
        ctx.toast("warning", "Usage: /reasoning <high|medium|low|none>")
        return True
    ctx.controller.enqueue_command("set_reasoning", effort=norm)
    ctx.toast("success", f"Reasoning set to {norm}")
    return True


def _cmd_yolo(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("toggle_auto_approve")
    return True


def _cmd_allow_tool(ctx: SlashContext, arg: str, head: str) -> bool:
    arg = arg.strip()
    if not arg:
        ctx.toast("warning", "Usage: /allow-tool <tool-name> [command-prefix | path]")
        return True
    parts = arg.split(None, 1)
    tool = parts[0]
    if len(parts) > 1 and parts[1].strip():
        ctx.controller.enqueue_command("allow_tool", tool=tool, scope=parts[1].strip())
    else:
        ctx.controller.enqueue_command("allow_tool", tool=tool)
    return True


def _cmd_disallow_tool(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.toast("warning", "Usage: /disallow-tool <tool-name>")
        return True
    ctx.controller.enqueue_command("disallow_tool", tool=arg)
    return True


def _cmd_allowed_tools(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("list_allowed_tools")
    return True


def _cmd_undo(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("send_message", text="/undo")
    return True


def _cmd_rewind(ctx: SlashContext, arg: str, head: str) -> bool:
    user_rows = [it for it in ctx.reducer.timeline if it.get("kind") == "user"]
    if not user_rows:
        ctx.toast("warning", "No turns to rewind to yet.")
        return True

    tokens = arg.split() if arg else []
    if not tokens:
        # No argument → list the turns the user can jump back to.
        lines = []
        for i, it in enumerate(user_rows, 1):
            lines.append(f"  {i}: {checkpoint_label(it.get('text'))}")
        ctx.toast(
            "info",
            "Rewind to which turn? Use /rewind <n> [--files]\n" + "\n".join(lines),
        )
        return True

    try:
        turn = int(tokens[0])
    except ValueError:
        ctx.toast("warning", "Usage: /rewind <turn> [--files]")
        return True

    restore_files = any(t.lower() in ("--files", "files", "-f") for t in tokens[1:])
    if turn < 1 or turn > len(user_rows):
        ctx.toast("warning", f"Invalid turn {turn}. Valid: 1–{len(user_rows)}.")
        return True

    # Truncate the local timeline, then ask the agent to truncate its history.
    ctx.rewind_timeline(turn)
    ctx.controller.enqueue_command("rewind", turn=turn, files=restore_files)
    return True


def _cmd_persona(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg or arg == "list":
        ctx.controller.enqueue_command("list_personas")
        ctx.show_palette("personas")
    else:
        ctx.controller.enqueue_command("set_persona", persona=arg)
    return True


def _cmd_mcp(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg or arg == "list":
        ctx.controller.enqueue_command("list_mcp_servers")
        ctx.show_palette("mcp")
    else:
        ctx.controller.enqueue_command("toggle_mcp_server", server=arg)
    return True


def _cmd_skills(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg or arg == "list":
        ctx.controller.enqueue_command("list_skills")
        ctx.show_palette("skills")
    else:
        ctx.controller.enqueue_command("send_message", text=f"/skills {arg}")
    return True


def _cmd_verbose(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.toggle_verbose()
    return True


def _cmd_think(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.reveal_reasoning()
    return True


def _cmd_tokens(ctx: SlashContext, arg: str, head: str) -> bool:
    # Refresh the status bar / panels, then surface a live usage summary.
    # The numbers are mirrored onto the session by the bridge's ``status``
    # events, so we can render them client-side without another round-trip.
    ctx.controller.enqueue_command("get_state")
    s = ctx.reducer.session
    used = s.ctx_used
    limit = s.ctx_limit
    pct = (used / limit * 100) if limit else 0.0
    total = s.prompt_tokens + s.completion_tokens
    pinned = s.context_files or []
    cost_line = f"${s.cost_usd:.4f}" + (f" / ${s.budget_usd:.2f} budget" if s.budget_usd else "")
    lines = [
        "Session usage",
        f"  Model:       {s.model or '(unknown)'}",
        f"  Context:     {used:,} / {limit:,} tokens ({pct:.0f}%)",
        f"  Prompt:      {s.prompt_tokens:,}",
        f"  Completion:  {s.completion_tokens:,}",
        f"  Total:       {total:,}",
        f"  Cost:        {cost_line}",
        f"  Pinned:      {len(pinned)} file(s)",
    ]
    ctx.toast("info", "\n".join(lines))
    return True


def _cmd_context(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("get_state")
    ctx.show_context()
    return True


def _cmd_code_search(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.toast("warning", "Usage: /code-search <query>")
    else:
        ctx.controller.enqueue_command("search_codebase", query=arg)
    return True


def _cmd_agents(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("get_state")
    ctx.toast("info", "Agents panel refreshed")
    return True


def _cmd_tasks(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("get_tasks")
    return True


def _cmd_show(ctx: SlashContext, arg: str, head: str) -> bool:
    # When invoked via alias (e.g. /version, /models), use the alias as the topic.
    topic = arg.lower() if head == "show" and arg else head.lower()
    if not topic:
        ctx.toast("warning", "Usage: /show <version|models|cost|info|config|system|tasks|plan>")
        return True
    topic = "models" if topic in ("providers", "models") else topic
    # `/show tasks` prints the text listing via the server's reference handler
    # (single source of truth); the dedicated `/tasks` command still refreshes
    # the panel. `plan` has no reference topic, so it keeps its panel refresh.
    if topic == "plan":
        ctx.controller.enqueue_command("get_plan")
    else:
        ctx.controller.enqueue_command("reference", topic=topic)
    return True


def _cmd_plan(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.controller.enqueue_command("get_plan")
    return True


def _cmd_exit(ctx: SlashContext, arg: str, head: str) -> bool:
    if not ctx.confirm_exit():
        ctx.toast("warning", "Type /exit again to confirm shutdown (resets in 5s)")
    return True


def _cmd_export(ctx: SlashContext, arg: str, head: str) -> bool:
    default_name = f"coderAI-session-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}.md"
    target = arg or str(Path.cwd() / default_name)
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(timeline_to_markdown(ctx.reducer.timeline), encoding="utf-8")
        ctx.toast("success", f"Exported to {target}")
    except OSError as e:
        ctx.toast("warning", f"Export failed: {e}")
    return True


def _cmd_search(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.show_search()
    else:
        ctx.set_search_filter(arg)
        ctx.show_search()
    return True


def _cmd_pin(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.toast("warning", "Usage: /pin <path>")
    else:
        ctx.controller.enqueue_command("manage_context", action="add", path=arg)
    return True


def _cmd_unpin(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.toast("warning", "Usage: /unpin <path>")
    else:
        ctx.controller.enqueue_command("manage_context", action="remove", path=arg)
    return True


def _cmd_copy(ctx: SlashContext, arg: str, head: str) -> bool:
    from coderAI.tui.clipboard import copy_to_clipboard_osc52, copy_fallback_file

    last = _find_last_assistant(ctx.reducer.timeline)
    if not last:
        ctx.toast("warning", "No assistant response to copy")
    else:
        copy_to_clipboard_osc52(last)
        copy_fallback_file(last, ctx.toast)
        ctx.toast("info", f"Sent {len(last):,} chars via OSC-52 + temp file")
    return True


def _cmd_retry(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.retry_agent()
    return True


def _cmd_resume(ctx: SlashContext, arg: str, head: str) -> bool:
    # No argument → open the session picker; with an id → resume directly.
    ctx.resume_session(arg.strip() or None)
    return True


def _cmd_kill(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg:
        ctx.toast("warning", "Usage: /kill <agent-id-or-name>")
        return True
    agents = ctx.reducer.session.agents
    target_id = arg
    for aid, info in agents.items():
        if info.name == arg or aid == arg:
            target_id = aid
            break
    ctx.controller.enqueue_command("cancel_agent", agentId=target_id)
    ctx.toast("info", f"Cancelling agent: {arg}")
    return True


def _cmd_init(ctx: SlashContext, arg: str, head: str) -> bool:
    ctx.toast("info", "Scaffolding .coderai/ project directory…")
    ctx.controller.enqueue_command("init_project")
    return True


# ── registry ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandSpec:
    """A slash command: its handler, all invocable names, and help copy.

    ``names[0]`` is the primary name shown in /help and the palette; the
    rest are aliases. The same spec is stored under every alias so the
    help menu can be derived from the registry (single source of truth).
    """

    handler: Callable[[SlashContext, str, str], bool]
    names: tuple[str, ...]
    desc: str


# Maps command names (normalised to lowercase) to the shared CommandSpec.
_SLASH_REGISTRY: Dict[str, CommandSpec] = {}

# Specs in registration order (one entry per command, not per alias).
COMMAND_SPECS: List[CommandSpec] = []


def _register(handler: Callable[[SlashContext, str, str], bool], *names: str, desc: str) -> None:
    spec = CommandSpec(handler=handler, names=names, desc=desc)
    COMMAND_SPECS.append(spec)
    for name in names:
        _SLASH_REGISTRY[name] = spec


_register(_cmd_help, "help", "?", desc="Open this command menu")
_register(_cmd_clear, "clear", desc="Wipe conversation & context")
_register(_cmd_compact, "compact", desc="Summarize long context")
_register(
    _cmd_model,
    "model",
    "change-model",
    "changemodel",
    "switch-model",
    desc="Open model picker · /model <name> · /model default <name>",
)
_register(
    _cmd_reasoning,
    "reasoning",
    "thinking",
    desc="Open reasoning picker · /reasoning <high|medium|low|none>",
)
_register(
    _cmd_yolo,
    "yolo",
    "auto-approve",
    "autoapprove",
    desc="Toggle auto-approve for high-risk tools",
)
_register(
    _cmd_allow_tool,
    "allow-tool",
    desc="Always allow a tool this session · high-risk needs a scope: /allow-tool run_command <prefix>",
)
_register(_cmd_disallow_tool, "disallow-tool", desc="Remove a per-session tool allowlist entry")
_register(_cmd_allowed_tools, "allowed-tools", desc="List tools already allowlisted this session")
_register(_cmd_undo, "undo", desc="Undo last tool action")
_register(
    _cmd_rewind,
    "rewind",
    desc="Rewind conversation to a past turn · /rewind <n> [--files]",
)
_register(_cmd_persona, "persona", desc="List or switch persona")
_register(_cmd_mcp, "mcp", desc="List MCP servers · toggle one on/off · /mcp <name>")
_register(_cmd_skills, "skills", desc="List workflows under .coderAI/skills/")
_register(_cmd_verbose, "verbose", desc="Toggle reasoning + expanded tool cards")
_register(_cmd_think, "think", "reveal", desc="Reveal the latest hidden reasoning as a toast")
_register(_cmd_tokens, "tokens", "status", desc="Show token usage, cost & context stats")
_register(_cmd_context, "context", desc="View pinned context files")
_register(
    _cmd_code_search,
    "code-search",
    "search-code",
    "cs",
    desc="Search the codebase semantically",
)
_register(_cmd_agents, "agents", desc="Refresh the agents tree (left panel)")
_register(_cmd_tasks, "tasks", "todos", "task", desc="Refresh TODO checklist panel")
_register(
    _cmd_show,
    "show",
    "version",
    "providers",
    "models",
    "cost",
    "pricing",
    "system",
    "diag",
    "diagnostics",
    "config",
    "info",
    desc="Reference info · type /show then a topic",
)
_register(_cmd_plan, "plan", desc="Show current execution plan (right panel)")
_register(_cmd_exit, "exit", "quit", desc="Shut down the agent")
_register(_cmd_export, "export", "save", desc="Export session to markdown")
_register(_cmd_search, "search", "find", desc="Search conversation transcript")
_register(_cmd_pin, "pin", desc="Pin a file to context · /pin <path>")
_register(_cmd_unpin, "unpin", desc="Unpin a file from context · /unpin <path>")
_register(_cmd_copy, "copy", desc="Copy last assistant response (OSC-52)")
_register(_cmd_retry, "retry", desc="Restart the agent after a crash")
_register(
    _cmd_resume,
    "resume",
    "sessions",
    desc="Resume a saved session · /resume [id]",
)
_register(_cmd_kill, "kill", "cancel-agent", desc="Cancel a sub-agent · /kill <id-or-name>")
_register(_cmd_init, "init", desc="Scaffold .coderai/ directory for the current project")


# ── dispatcher ────────────────────────────────────────────────────────


def handle_slash_command(
    raw: str,
    controller: "UIBridge",
    reducer: "EventReducer",
    *,
    show_palette: Callable[[Optional[str]], None],
    show_search: Callable[[], None],
    show_context: Callable[[], None],
    clear_context: Callable[[], None],
    toggle_verbose: Callable[[], None],
    reveal_reasoning: Callable[[], None],
    confirm_exit: Callable[[], bool],
    set_search_filter: Callable[[str], None],
    retry_agent: Callable[[], None],
    rewind_timeline: Callable[[int], None],
    resume_session: Callable[[Optional[str]], None],
) -> bool:
    """Dispatch a slash command. Returns True if handled."""

    parts = raw[1:].split(None, 1) if raw.startswith("/") else raw.split(None, 1)
    head = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    ctx = SlashContext(
        controller=controller,
        reducer=reducer,
        show_palette=show_palette,
        show_search=show_search,
        show_context=show_context,
        clear_context=clear_context,
        toggle_verbose=toggle_verbose,
        reveal_reasoning=reveal_reasoning,
        confirm_exit=confirm_exit,
        set_search_filter=set_search_filter,
        retry_agent=retry_agent,
        rewind_timeline=rewind_timeline,
        resume_session=resume_session,
    )

    entry = _SLASH_REGISTRY.get(head)
    if entry is not None:
        return entry.handler(ctx, arg, head)

    ctx.toast("warning", f"Unknown command: /{head} · type /help")
    return True


# ── helpers ───────────────────────────────────────────────────────────


def _find_last_assistant(timeline: List[Dict[str, Any]]) -> Optional[str]:
    for it in reversed(timeline):
        if it.get("kind") == "assistant":
            return it.get("content") or ""
    return None
