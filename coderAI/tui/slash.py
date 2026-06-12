"""Client-side slash command routing — registry-driven dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from coderAI.tui.export import timeline_to_markdown

if TYPE_CHECKING:
    from ..bridge.controller import UIBridge
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

    def toast(self, level: str, message: str) -> None:
        self.reducer._push(
            {"kind": "toast", "id": self.reducer.next_id(), "level": level, "message": message}
        )
        self.reducer._bump_refresh("append")
        self.reducer._notify()


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
    if not arg:
        ctx.toast("warning", "Usage: /allow-tool <tool-name>")
        return True
    ctx.controller.enqueue_command("allow_tool", tool=arg)
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


def _cmd_persona(ctx: SlashContext, arg: str, head: str) -> bool:
    if not arg or arg == "list":
        ctx.controller.enqueue_command("list_personas")
        ctx.show_palette("personas")
    else:
        ctx.controller.enqueue_command("set_persona", persona=arg)
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


def _cmd_status(ctx: SlashContext, arg: str, head: str) -> bool:
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
    if topic in ("tasks", "todos", "task"):
        ctx.controller.enqueue_command("get_tasks")
    elif topic == "plan":
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

# Maps command names (normalised to lowercase) to handler.
# Handler signature: (ctx, arg: str, head: str) -> bool
_SLASH_REGISTRY: Dict[str, Callable[[SlashContext, str, str], bool]] = {}


def _register(handler: Callable[[SlashContext, str, str], bool], *names: str) -> None:
    for name in names:
        _SLASH_REGISTRY[name] = handler


_register(_cmd_help, "help", "?")
_register(_cmd_clear, "clear")
_register(_cmd_compact, "compact")
_register(_cmd_model, "model", "change-model", "changemodel", "switch-model")
_register(_cmd_reasoning, "reasoning", "thinking")
_register(_cmd_yolo, "yolo", "auto-approve", "autoapprove")
_register(_cmd_allow_tool, "allow-tool")
_register(_cmd_disallow_tool, "disallow-tool")
_register(_cmd_allowed_tools, "allowed-tools")
_register(_cmd_undo, "undo")
_register(_cmd_persona, "persona")
_register(_cmd_skills, "skills")
_register(_cmd_verbose, "verbose")
_register(_cmd_think, "think", "reveal")
_register(_cmd_status, "tokens", "status", "context")
_register(_cmd_code_search, "code-search", "search-code", "cs")
_register(_cmd_agents, "agents")
_register(_cmd_tasks, "tasks", "todos", "task")
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
)
_register(_cmd_plan, "plan")
_register(_cmd_exit, "exit", "quit")
_register(_cmd_export, "export", "save")
_register(_cmd_search, "search", "find")
_register(_cmd_pin, "pin")
_register(_cmd_unpin, "unpin")
_register(_cmd_copy, "copy")
_register(_cmd_retry, "retry")
_register(_cmd_kill, "kill", "cancel-agent")
_register(_cmd_init, "init")


# ── dispatcher ────────────────────────────────────────────────────────


def handle_slash_command(
    raw: str,
    controller: "UIBridge",
    reducer: "EventReducer",
    *,
    show_help: Callable[[], None],
    show_model_menu: Callable[[], None],
    show_reasoning_menu: Callable[[], None],
    show_persona_menu: Callable[[], None],
    show_skills_menu: Callable[[], None],
    show_search: Callable[[], None],
    show_context: Callable[[], None],
    clear_context: Callable[[], None],
    toggle_verbose: Callable[[], None],
    reveal_reasoning: Callable[[], None],
    confirm_exit: Callable[[], bool],
    set_search_filter: Callable[[str], None],
    retry_agent: Callable[[], None],
) -> bool:
    """Dispatch a slash command. Returns True if handled."""

    parts = raw[1:].split(None, 1) if raw.startswith("/") else raw.split(None, 1)
    head = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    ctx = SlashContext(
        controller=controller,
        reducer=reducer,
        show_palette=lambda s: None,  # patched below
        show_search=show_search,
        show_context=show_context,
        clear_context=clear_context,
        toggle_verbose=toggle_verbose,
        reveal_reasoning=reveal_reasoning,
        confirm_exit=confirm_exit,
        set_search_filter=set_search_filter,
        retry_agent=retry_agent,
    )

    # Wire the palette callbacks through the section-aware helper.
    def _show_palette(section: str | None = None) -> None:
        if section is None:
            show_help()
        elif section == "models":
            show_model_menu()
        elif section == "reasoning":
            show_reasoning_menu()
        elif section == "personas":
            show_persona_menu()
        elif section == "skills":
            show_skills_menu()
        else:
            show_help()

    ctx.show_palette = _show_palette

    if head in ("help", "?"):
        show_help()
        return True

    entry = _SLASH_REGISTRY.get(head)
    if entry is not None:
        return entry(ctx, arg, head)

    ctx.toast("warning", f"Unknown command: /{head} · type /help")
    return True


# ── helpers ───────────────────────────────────────────────────────────


def _find_last_assistant(timeline: List[Dict[str, Any]]) -> Optional[str]:
    for it in reversed(timeline):
        if it.get("kind") == "assistant":
            return it.get("content") or ""
    return None
