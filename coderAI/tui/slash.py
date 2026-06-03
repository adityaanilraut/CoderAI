"""Client-side slash command routing."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from coderAI.tui.export import timeline_to_markdown

if TYPE_CHECKING:
    from ..bridge.controller import UIBridge
    from coderAI.tui.listeners import EventReducer


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
) -> bool:
    """Handle a slash command locally. Returns True if handled (don't send to agent)."""
    parts = raw[1:].split(None, 1) if raw.startswith("/") else raw.split(None, 1)
    head = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    def toast(level: str, message: str) -> None:
        reducer._push(
            {"kind": "toast", "id": reducer.next_id(), "level": level, "message": message}
        )
        reducer._bump_refresh("append")
        reducer._notify()

    if head in ("help", "?"):
        show_help()
        return True
    if head == "clear":
        clear_context()
        return True
    if head == "compact":
        controller.enqueue_command("compact_context")
        toast("info", "compacting context…")
        return True
    if head in ("model", "change-model", "changemodel", "switch-model"):
        sub = arg.split() if arg else []
        if not sub:
            controller.enqueue_command("list_models")
            show_model_menu()
            return True
        if sub[0].lower() == "default":
            target = " ".join(sub[1:]).strip()
            if not target:
                toast("warning", "Usage: /model default <name>")
                return True
            controller.enqueue_command("set_default_model", model=target)
            toast("success", f"Default model set to {target}")
            return True
        controller.enqueue_command("set_model", model=arg)
        toast("success", f"Model set to {arg}")
        return True
    if head in ("reasoning", "thinking"):
        if not arg:
            show_reasoning_menu()
            return True
        norm = arg.lower()
        if norm not in ("high", "medium", "low", "none"):
            toast("warning", "Usage: /reasoning <high|medium|low|none>")
            return True
        controller.enqueue_command("set_reasoning", effort=norm)
        toast("success", f"Reasoning set to {norm}")
        return True
    if head in ("yolo", "auto-approve", "autoapprove"):
        controller.enqueue_command("toggle_auto_approve")
        return True
    if head == "allow-tool":
        if not arg:
            toast("warning", "Usage: /allow-tool <tool-name>")
            return True
        controller.enqueue_command("allow_tool", tool=arg)
        return True
    if head == "disallow-tool":
        if not arg:
            toast("warning", "Usage: /disallow-tool <tool-name>")
            return True
        controller.enqueue_command("disallow_tool", tool=arg)
        return True
    if head == "allowed-tools":
        controller.enqueue_command("list_allowed_tools")
        return True
    if head == "undo":
        controller.enqueue_command("send_message", text=raw)
        return True
    if head == "persona":
        if not arg or arg == "list":
            controller.enqueue_command("list_personas")
            show_persona_menu()
        else:
            controller.enqueue_command("set_persona", persona=arg)
        return True
    if head == "skills":
        if not arg or arg == "list":
            controller.enqueue_command("list_skills")
            show_skills_menu()
        else:
            controller.enqueue_command("send_message", text=raw)
        return True
    if head == "verbose":
        toggle_verbose()
        return True
    if head in ("think", "reveal"):
        reveal_reasoning()
        return True
    if head in ("tokens", "status", "context"):
        controller.enqueue_command("get_state")
        show_context()
        return True
    if head in ("code-search", "search-code", "cs"):
        if not arg:
            toast("warning", "Usage: /code-search <query>")
        else:
            controller.enqueue_command("search_codebase", query=arg)
        return True
    if head == "agents":
        controller.enqueue_command("get_state")
        toast("info", "Agents panel refreshed")
        return True
    if head == "show":
        topic = arg.lower()
        if not topic:
            toast("warning", "Usage: /show <version|models|cost|info|config|system|tasks|plan>")
            return True
        if topic == "plan":
            controller.enqueue_command("get_plan")
        else:
            controller.enqueue_command("reference", topic=topic)
        return True
    if head in (
        "version",
        "providers",
        "cost",
        "pricing",
        "system",
        "diag",
        "diagnostics",
        "config",
        "info",
        "tasks",
        "todos",
        "task",
    ):
        topic = "models" if head == "providers" else head
        controller.enqueue_command("reference", topic=topic)
        return True
    if head == "plan":
        controller.enqueue_command("get_plan")
        return True
    if head in ("exit", "quit"):
        if not confirm_exit():
            toast("warning", "Type /exit again to confirm shutdown (resets in 5s)")
        return True
    if head in ("export", "save"):
        default_name = (
            f"coderAI-session-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}.md"
        )
        target = arg or str(Path.home() / "Desktop" / default_name)
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(timeline_to_markdown(reducer.timeline), encoding="utf-8")
            toast("success", f"Exported to {target}")
        except OSError as e:
            toast("warning", f"Export failed: {e}")
        return True
    if head in ("search", "find"):
        if not arg:
            show_search()
        else:
            set_search_filter(arg)
            show_search()
        return True
    if head == "pin":
        if not arg:
            toast("warning", "Usage: /pin <path>")
        else:
            controller.enqueue_command("manage_context", action="add", path=arg)
        return True
    if head == "unpin":
        if not arg:
            toast("warning", "Usage: /unpin <path>")
        else:
            controller.enqueue_command("manage_context", action="remove", path=arg)
        return True
    if head == "copy":
        last = _find_last_assistant(reducer.timeline)
        if not last:
            toast("warning", "No assistant response to copy")
        else:
            _copy_osc52(last)
            toast("info", f"Sent {len(last):,} chars via OSC-52 — paste to verify")
        return True
    if head == "theme":
        theme_name = arg.lower()
        if theme_name not in ("dark", "light"):
            toast("warning", "Usage: /theme <dark|light>")
            return True
        os.environ["CODERAI_THEME"] = theme_name
        cfg_path = Path.home() / ".coderAI" / "config.json"
        try:
            cfg: Dict[str, Any] = {}
            if cfg_path.is_file():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["theme"] = theme_name
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            toast("success", f"Theme persisted as {theme_name}. Restart chat to apply.")
        except OSError as e:
            toast("warning", f"Theme save failed: {e}")
        return True
    toast("warning", f"Unknown command: /{head} · type /help")
    return True


def _find_last_assistant(timeline: List[Dict[str, Any]]) -> Optional[str]:
    for it in reversed(timeline):
        if it.get("kind") == "assistant":
            return it.get("content") or ""
    return None


def _copy_osc52(text: str) -> None:
    import base64
    import sys

    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\033]52;c;{encoded}\007")
    sys.stdout.flush()
