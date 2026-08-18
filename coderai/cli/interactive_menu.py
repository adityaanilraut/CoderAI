"""Interactive selection and inspection menus (/model, /sessions, /skills, /mcp, /config, /tokens, /history)."""

from __future__ import annotations

from typing import Any

from coderai.core.common.model_capabilities import (
    CURATED_MODELS,
    format_capability_badges,
    get_model_badges,
)
from coderai.core.prompt import list_skills
from coderai.core.session import SessionEntry, SessionManager, SessionMessage

try:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Panel = None  # type: ignore[assignment,misc]
    Table = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False


def _format_badges_markup(badges: list[str]) -> str:
    parts: list[str] = []
    for b in badges:
        if b == "Thinking":
            parts.append("[bold magenta][Thinking][/]")
        elif b == "Fast":
            parts.append("[bold yellow][Fast][/]")
        elif b == "Multimodal":
            parts.append("[bold cyan][Multimodal][/]")
        else:
            parts.append(f"[bold white][{b}][/]")
    return " ".join(parts)


def select_model_interactive(console: Any | None, current_model: str) -> str:
    """Prompt the user with an interactive model selection menu."""
    if console is not None and _RICH and Table is not None:
        table = Table(
            title="✨ Select Active Model (Frontier Coding & Reasoning)",
            border_style="magenta",
            header_style="bold magenta",
        )
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Category", style="dim", width=18)
        table.add_column("Model Name", style="bold white", width=22)
        table.add_column("Capabilities", width=26)
        table.add_column("Description", style="dim")
        table.add_column("Active", justify="center", width=8)

        for idx, (name, desc, category) in enumerate(CURATED_MODELS, 1):
            is_cur = "[bold green]✓ Active[/]" if name == current_model else "—"
            badges = get_model_badges(name)
            badges_str = _format_badges_markup(badges)
            table.add_row(str(idx), category, name, badges_str, desc, is_cur)

        table.add_section()
        table.add_row(
            str(len(CURATED_MODELS) + 1),
            "Custom",
            "Custom...",
            "[dim]—[/]",
            "Enter any custom model identifier or provider endpoint",
            "",
        )
        console.print(table)
    else:
        print("\n--- Select Active Model ---")
        current_category = ""
        for idx, (name, desc, category) in enumerate(CURATED_MODELS, 1):
            if category != current_category:
                current_category = category
                print(f"\n[{category}]")
            cur_marker = " (Active)" if name == current_model else ""
            badges_plain = format_capability_badges(name)
            badge_str = f" {badges_plain}" if badges_plain else ""
            print(f"  {idx:2}. {name:<22}{badge_str:<26} {desc}{cur_marker}")
        print(f"\n  {len(CURATED_MODELS) + 1:2}. Custom (enter custom model name)")

    try:
        choice = input(
            f"\nSelect model [1-{len(CURATED_MODELS) + 1}, or name] (Enter to keep '{current_model}'): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return current_model

    if not choice:
        return current_model

    if choice.isdigit():
        num = int(choice)
        if 1 <= num <= len(CURATED_MODELS):
            return CURATED_MODELS[num - 1][0]
        if num == len(CURATED_MODELS) + 1:
            try:
                custom_name = input("Enter custom model identifier: ").strip()
                return custom_name if custom_name else current_model
            except (EOFError, KeyboardInterrupt):
                return current_model

    from coderai.cli.fuzzy import fuzzy_filter

    # Direct model name typed by user - attempt fuzzy match first
    model_names = [name for name, _, _ in CURATED_MODELS]
    fuzzy_models = fuzzy_filter(choice, model_names, limit=1)
    if fuzzy_models:
        return fuzzy_models[0]

    return choice


def select_session_interactive(console: Any | None, sessions: list[SessionEntry]) -> str | None:
    """Display interactive sessions list and allow selecting a session to resume, delete, or fork."""
    if not sessions:
        if console is not None and _RICH:
            console.print("[dim]No saved sessions found in workspace.[/]")
        else:
            print("No saved sessions found in workspace.")
        return None

    if console is not None and _RICH and Table is not None:
        table = Table(title="Interactive Saved Sessions Menu", border_style="blue")
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Session ID", style="bold white", width=16)
        table.add_column("Status", style="yellow", width=12)
        table.add_column("Plan", style="magenta", width=6)
        table.add_column("Tokens", style="green", width=10)
        table.add_column("Summary", style="white")

        for idx, s in enumerate(sessions, 1):
            table.add_row(
                str(idx),
                s.id[:14],
                s.status,
                "✓" if s.plan_mode else "—",
                f"{s.active_tokens:,}",
                (s.summary or "(no summary)")[:50],
            )
        console.print(table)
        console.print(
            "[dim]Actions: [bold cyan]<num>[/] resume • [bold red]d <num>[/] delete • [bold yellow]f <num>[/] fork • [bold]Enter[/] cancel[/]"
        )
    else:
        print("\n--- Saved Sessions ---")
        for idx, s in enumerate(sessions, 1):
            plan_str = "[plan]" if s.plan_mode else "      "
            print(
                f"  {idx:2}. {s.id[:14]}  {s.status:10} {plan_str}  {s.active_tokens:6} tokens  {(s.summary or '')[:40]}"
            )
        print("Actions: <num> resume | d <num> delete | f <num> fork | Enter cancel")

    try:
        raw_choice = input(
            f"\nSelect session action [1-{len(sessions)}, d <num>, f <num>, or query]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw_choice:
        return None

    # Handle delete action: "d 1" or "del 1"
    if (
        raw_choice.startswith(("d ", "del ", "delete ", "d", "del", "delete"))
        and len(raw_choice.split()) > 1
    ):
        parts = raw_choice.split()
        if parts[1].isdigit():
            idx = int(parts[1])
            if 1 <= idx <= len(sessions):
                return f"delete:{sessions[idx - 1].id}"

    # Handle fork action: "f 1" or "fork 1"
    if raw_choice.startswith(("f ", "fork ", "f", "fork")) and len(raw_choice.split()) > 1:
        parts = raw_choice.split()
        if parts[1].isdigit():
            idx = int(parts[1])
            if 1 <= idx <= len(sessions):
                return f"fork:{sessions[idx - 1].id}"

    # Standard resume by number
    if raw_choice.isdigit():
        idx = int(raw_choice)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1].id

    # If user typed raw session id or search query, use fuzzy search
    from coderai.cli.fuzzy import fuzzy_filter

    matched_sessions = fuzzy_filter(
        raw_choice,
        sessions,
        key_func=lambda s: f"{s.id} {s.summary or ''}",
        limit=1,
    )
    if matched_sessions:
        return matched_sessions[0].id

    matched = next((s.id for s in sessions if s.id.startswith(raw_choice)), None)
    return matched


def prompt_plan_implementation(console: Any | None) -> str:
    """Display interactive post-plan decision prompt.

    Returns:
      "execute" -> execute plan now (exit plan mode)
      "refine"  -> refine plan
      "stay"    -> stay in plan mode
    """
    if console is not None and _RICH and Panel is not None:
        body = (
            "[bold white]A plan has been proposed for your request.[/]\n\n"
            "  [bold green][1][/] [bold]Yes, execute plan now[/]  [dim]— turn off Plan Mode and begin implementation[/]\n"
            "  [bold yellow][2][/] [bold]Refine plan[/]            [dim]— specify adjustments and stay in Plan Mode[/]\n"
            "  [bold cyan][3][/] [bold]No, stay in plan mode[/]  [dim]— keep exploring before executing[/]"
        )
        panel = Panel(
            body,
            title="[bold yellow]✨ Plan Ready — Next Action[/]",
            border_style="yellow",
        )
        console.print()
        console.print(panel)
    else:
        print("\n--- Plan Ready ---")
        print("  1. Yes, execute plan now (turn off Plan Mode and begin implementation)")
        print("  2. Refine plan (specify adjustments and stay in Plan Mode)")
        print("  3. No, stay in plan mode (keep exploring)")

    try:
        raw = input("\nChoose action [1/2/3] (default 1): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "stay"

    if raw in ("1", "y", "yes", "execute", ""):
        return "execute"
    if raw in ("2", "r", "refine"):
        return "refine"
    return "stay"


def select_undo_interactive(
    console: Any | None, targets: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """Display interactive turn selector and restore mode picker for /undo.

    Returns (selected_target, mode) where mode is:
      - "restore_both"
      - "restore_conversation_only"
      - "restore_code_only"
    """
    if not targets:
        return None, "restore_both"

    if len(targets) == 1:
        chosen_target = targets[0]
    else:
        if console is not None and _RICH and Table is not None:
            table = Table(title="Available Undo Checkpoints", border_style="cyan")
            table.add_column("Turn #", style="bold cyan", width=8)
            table.add_column("Prompt Snippet", style="white")
            table.add_column("Checkpoint Git Hash", style="dim", width=18)
            table.add_column("Has Code Backup", style="green", width=16)

            for t in targets:
                ckpt = t.get("checkpoint_hash") or "—"
                can_code = "[bold green]✓ Yes[/]" if t.get("can_restore_code") else "[dim]—[/]"
                table.add_row(str(t["index"]), t["prompt"][:60], ckpt[:12], can_code)
            console.print(table)
        else:
            print("\n--- Available Undo Checkpoints ---")
            for t in targets:
                ckpt = (t.get("checkpoint_hash") or "")[:10]
                print(f"  {t['index']:2}. {t['prompt'][:50]} (ckpt: {ckpt})")

        try:
            choice_str = input(
                f"\nSelect turn to undo to [1-{len(targets)}] (default {len(targets)}: latest, Enter cancel): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None, "restore_both"

        if not choice_str:
            chosen_target = targets[-1]
        elif choice_str.isdigit() and 1 <= int(choice_str) <= len(targets):
            chosen_target = targets[int(choice_str) - 1]
        else:
            return None, "restore_both"

    # Choose restore mode
    if console is not None and _RICH:
        console.print(
            "\n[bold]Select restore mode:[/]\n"
            "  [bold cyan]1.[/] [bold]Restore Both[/] [dim]— Revert files on disk and rollback conversation history (Default)[/]\n"
            "  [bold cyan]2.[/] [bold]Restore Conversation Only[/] [dim]— Rollback message history, keep code changes on disk[/]\n"
            "  [bold cyan]3.[/] [bold]Restore Code Only[/] [dim]— Revert files on disk, keep conversation history[/]"
        )
    else:
        print("\nSelect restore mode:")
        print("  1. Restore Both (files on disk + conversation history) [Default]")
        print("  2. Restore Conversation Only (keep code changes on disk)")
        print("  3. Restore Code Only (revert files, keep conversation)")

    try:
        mode_str = input("Choose mode [1/2/3] (default 1): ").strip()
    except (EOFError, KeyboardInterrupt):
        return chosen_target, "restore_both"

    if mode_str == "2":
        return chosen_target, "restore_conversation_only"
    elif mode_str == "3":
        return chosen_target, "restore_code_only"
    return chosen_target, "restore_both"


def render_skills_interactive(console: Any | None, project_root: str) -> None:
    """Render the interactive skills viewer."""
    skills = list_skills(project_root)
    if not skills:
        if console is not None and _RICH:
            console.print("[dim]No active skills discovered in workspace or global config.[/]")
        else:
            print("No active skills discovered in workspace or global config.")
        return

    if console is not None and _RICH and Table is not None:
        table = Table(title=f"Discovered Skills ({len(skills)})", border_style="yellow")
        table.add_column("Skill Name", style="bold cyan", width=24)
        table.add_column("Description", style="white")
        table.add_column("Location", style="dim", width=36)

        for sk in skills:
            table.add_row(
                f"⚡ {sk.get('name', '')}",
                sk.get("description", "(no description)"),
                sk.get("location", ""),
            )
        console.print(table)
        console.print(
            "[dim]Use [bold cyan]/skill <name>[/] to load a skill into active session.[/]\n"
        )
    else:
        print(f"\n--- Discovered Skills ({len(skills)}) ---")
        for sk in skills:
            print(
                f"  ⚡ {sk.get('name', '')}: {sk.get('description', '')} [{sk.get('location', '')}]"
            )
        print("Use /skill <name> to load a skill.\n")


def render_mcp_prompts(console: Any | None, mgr: SessionManager) -> None:
    """Render table of all discovered prompts from MCP servers."""
    prompts = mgr.mcp_manager.get_prompts()
    if not prompts:
        if console is not None and _RICH:
            console.print("[dim]No MCP prompts discovered on connected servers.[/]")
        else:
            print("No MCP prompts discovered on connected servers.")
        return

    if console is not None and _RICH and Table is not None:
        table = Table(title=f"Discovered MCP Prompts ({len(prompts)})", border_style="magenta")
        table.add_column("Server", style="dim cyan", width=16)
        table.add_column("Prompt Name", style="bold magenta", width=24)
        table.add_column("Description", style="white")
        table.add_column("Arguments", style="dim")

        for p in prompts:
            defn = p.get("definition") or {}
            args = defn.get("arguments") or []
            arg_names = [a.get("name", "") for a in args if isinstance(a, dict)]
            args_str = ", ".join(arg_names) if arg_names else "none"
            table.add_row(
                p.get("server_name", ""),
                p.get("original_name", ""),
                defn.get("description", "(no description)"),
                args_str,
            )
        console.print(table)
    else:
        print(f"\n--- Discovered MCP Prompts ({len(prompts)}) ---")
        for p in prompts:
            defn = p.get("definition") or {}
            print(
                f"  [{p.get('server_name')}] {p.get('original_name')} - {defn.get('description', '')}"
            )


async def render_mcp_resources_async(
    console: Any | None, mgr: SessionManager, uri: str | None = None
) -> None:
    """Render table of discovered MCP resources or fetch and display content for a given URI."""
    if uri:
        res = await mgr.mcp_manager.read_resource(uri)
        contents = res.get("contents") or []
        if not contents:
            err = res.get("error") or f"No content found for resource URI: {uri}"
            if console is not None and _RICH:
                console.print(f"[bold red]{err}[/]")
            else:
                print(err)
            return

        for item in contents:
            item_uri = item.get("uri", uri)
            text = item.get("text") or item.get("blob") or "(empty content)"
            if console is not None and _RICH and Panel is not None:
                panel = Panel(
                    str(text)[:2000],
                    title=f"[bold cyan]MCP Resource:[/] {item_uri}",
                    border_style="cyan",
                )
                console.print(panel)
            else:
                print(f"\n--- MCP Resource: {item_uri} ---")
                print(str(text)[:2000])
        return

    resources = mgr.mcp_manager.get_resources()
    if not resources:
        if console is not None and _RICH:
            console.print("[dim]No MCP resources discovered on connected servers.[/]")
        else:
            print("No MCP resources discovered on connected servers.")
        return

    if console is not None and _RICH and Table is not None:
        table = Table(title=f"Discovered MCP Resources ({len(resources)})", border_style="cyan")
        table.add_column("Server", style="dim cyan", width=16)
        table.add_column("Resource Name", style="bold cyan", width=24)
        table.add_column("URI", style="green", width=30)
        table.add_column("Description", style="white")

        for r in resources:
            defn = r.get("definition") or {}
            table.add_row(
                r.get("server_name", ""),
                r.get("original_name", ""),
                defn.get("uri", ""),
                defn.get("description", "(no description)"),
            )
        console.print(table)
        console.print(
            "[dim]Use [bold cyan]/mcp resources <uri>[/] to inspect a specific resource content.[/]\n"
        )
    else:
        print(f"\n--- Discovered MCP Resources ({len(resources)}) ---")
        for r in resources:
            defn = r.get("definition") or {}
            print(
                f"  [{r.get('server_name')}] {r.get('original_name')} ({defn.get('uri', '')}) - {defn.get('description', '')}"
            )


def render_mcp_interactive(console: Any | None, mgr: SessionManager) -> None:
    """Render connected Model Context Protocol (MCP) servers and tools."""
    mcp_mgr = mgr.mcp_manager
    statuses = (
        mcp_mgr.get_status()
        if hasattr(mcp_mgr, "get_status")
        else getattr(mcp_mgr, "server_statuses", [])
    )
    tools = mcp_mgr.list_tools() if hasattr(mcp_mgr, "list_tools") else []

    if not statuses and not tools:
        if console is not None and _RICH:
            console.print(
                "[dim]No MCP servers configured or connected. Configure in .coderai/settings.json under 'mcpServers'.[/]"
            )
        else:
            print("No MCP servers configured or connected. Configure in .coderai/settings.json.")
        return

    if console is not None and _RICH and Table is not None:
        # Server status table
        table = Table(title="Connected MCP Servers", border_style="green")
        table.add_column("Server", style="bold cyan", width=18)
        table.add_column("Status", style="bold green", width=12)
        table.add_column("Tools", style="white", width=8)
        table.add_column("Prompts", style="dim", width=8)
        table.add_column("Resources", style="dim", width=10)

        for s in statuses:
            status_style = "bold green" if s.status == "ready" else "bold red"
            table.add_row(
                s.name,
                f"[{status_style}]{s.status}[/]",
                str(s.tool_count),
                str(s.prompt_count),
                str(s.resource_count),
            )
        console.print(table)

        if tools:
            tool_table = Table(title="Discovered MCP Tools", border_style="cyan")
            tool_table.add_column("Server", style="dim", width=16)
            tool_table.add_column("Tool Name", style="bold cyan", width=24)
            tool_table.add_column("Description", style="white")

            for t in tools:
                tool_table.add_row(
                    t.server_name,
                    t.original_name,
                    t.definition.get("description", "(no description)"),
                )
            console.print(tool_table)
    else:
        print("\n--- Connected MCP Servers ---")
        for s in statuses:
            print(f"  [{s.name}] status={s.status} tools={s.tool_count}")
        if tools:
            print("\n--- MCP Tools ---")
            for t in tools:
                print(
                    f"  {t.server_name}::{t.original_name} - {t.definition.get('description', '')}"
                )


def render_config_interactive(console: Any | None, project_root: str) -> None:
    """Display active workspace and user configuration."""
    from coderai.core.settings import (
        get_project_settings_path,
        get_user_settings_path,
        resolve_current_settings,
    )

    settings = resolve_current_settings(project_root)
    user_path = get_user_settings_path()
    proj_path = get_project_settings_path(project_root)

    if console is not None and _RICH and Panel is not None and Table is not None:
        table = Table.grid(padding=(0, 2))
        table.add_column("Key", style="dim cyan", width=22)
        table.add_column("Value", style="bold white")

        table.add_row("Active Model:", str(settings.get("model", "default")))
        table.add_row("Base URL:", str(settings.get("base_url", "default")))
        table.add_row("Permission Mode:", str(settings.get("permissions_default", "askAll")))
        table.add_row("Reasoning Effort:", str(settings.get("reasoning_effort", "adaptive")))
        table.add_row("Timeout (s):", str(settings.get("timeout_seconds", 300)))
        table.add_row("Context Window:", f"{settings.get('context_window', 262144):,} tokens")

        allows = settings.get("permissions_allow") or []
        allows_str = ", ".join(allows) if allows else "none"
        table.add_row("Allowed Scopes:", f"[bold green]{allows_str}[/]")

        mcp_servers = list(settings.get("mcp_servers", {}).keys())
        table.add_row("Configured MCP Servers:", ", ".join(mcp_servers) if mcp_servers else "none")
        table.add_row("User Config File:", f"[dim]{user_path}[/]")
        table.add_row("Project Config File:", f"[dim]{proj_path}[/]")

        panel = Panel(
            table,
            title="[bold cyan]CoderAI Active Configuration[/]",
            border_style="cyan",
            padding=(0, 1),
        )
        console.print()
        console.print(panel)
    else:
        print("\n--- CoderAI Configuration ---")
        for k, v in settings.items():
            print(f"  {k}: {v}")
        print(f"  User Config: {user_path}")
        print(f"  Project Config: {proj_path}\n")


def render_token_breakdown(
    console: Any | None, mgr: SessionManager, session_id: str | None
) -> None:
    """Display session token analytics and context window usage."""
    if not session_id:
        if console is not None and _RICH:
            console.print("[dim]No active session to display token analytics.[/]")
        else:
            print("No active session.")
        return

    entry = mgr.get_session(session_id)
    if not entry:
        if console is not None and _RICH:
            console.print("[dim]Session not found.[/]")
        return

    usage = entry.usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    active_tokens = entry.active_tokens

    # Default context window ~256k
    max_context = 256 * 1024
    pct_used = (active_tokens / max_context) * 100

    if console is not None and _RICH and Panel is not None and Table is not None:
        table = Table.grid(padding=(0, 2))
        table.add_column("Metric", style="dim cyan", width=22)
        table.add_column("Tokens", style="bold white")

        table.add_row("Prompt Tokens:", f"{prompt_tokens:,}")
        table.add_row("Completion Tokens:", f"{completion_tokens:,}")
        if cached_tokens > 0:
            table.add_row("Cached Tokens:", f"[green]{cached_tokens:,}[/]")
        table.add_row("Total Session Tokens:", f"[bold cyan]{total_tokens:,}[/]")
        table.add_row(
            "Active Working Context:",
            f"[bold green]{active_tokens:,}[/] / {max_context:,} ({pct_used:.1f}%)",
        )

        if entry.usage_per_model:
            table.add_row("Usage by Model:", "")
            for model_name, m_usage in entry.usage_per_model.items():
                m_total = m_usage.get("total_tokens", 0) if isinstance(m_usage, dict) else 0
                table.add_row(f"  • {model_name}:", f"{m_total:,} tokens")

        panel = Panel(
            table,
            title=f"[bold green]Token Usage & Context Analytics[/] [dim]({session_id[:12]})[/]",
            border_style="green",
            padding=(0, 1),
        )
        console.print()
        console.print(panel)
    else:
        print(f"\n--- Token Usage: {session_id[:12]} ---")
        print(f"  Prompt Tokens:     {prompt_tokens:,}")
        print(f"  Completion Tokens: {completion_tokens:,}")
        print(f"  Total Tokens:      {total_tokens:,}")
        print(f"  Active Context:    {active_tokens:,} ({pct_used:.1f}%)\n")


def render_session_history(
    console: Any | None, mgr: SessionManager, session_id: str | None
) -> None:
    """Display a concise turn-by-turn history timeline for the active session."""
    if not session_id:
        if console is not None and _RICH:
            console.print("[dim]No active session to display history.[/]")
        else:
            print("No active session.")
        return

    messages: list[SessionMessage] = mgr.list_session_messages(session_id)
    if not messages:
        if console is not None and _RICH:
            console.print("[dim]Session history is empty.[/]")
        else:
            print("Session history is empty.")
        return

    if console is not None and _RICH and Table is not None:
        table = Table(title=f"Session History Timeline ({session_id[:12]})", border_style="cyan")
        table.add_column("Turn", style="bold cyan", width=6)
        table.add_column("Role", style="magenta", width=10)
        table.add_column("Snippet / Action", style="white")

        for idx, m in enumerate(messages, 1):
            if m.role == "system":
                continue
            role_style = (
                "bold cyan"
                if m.role == "user"
                else ("bold green" if m.role == "assistant" else "yellow")
            )
            preview = (m.content or "").strip().splitlines()[0] if m.content else "(empty)"
            if m.role == "assistant" and m.tool_calls:
                fn_names = [
                    tc.get("function", {}).get("name", "")
                    if isinstance(tc, dict)
                    else getattr(getattr(tc, "function", None), "name", "")
                    for tc in m.tool_calls
                ]
                preview = f"→ called {', '.join(filter(None, fn_names))}"
            table.add_row(str(idx), f"[{role_style}]{m.role}[/]", preview[:80])
        console.print(table)
    else:
        print(f"\n--- Session History ({session_id[:12]}) ---")
        for idx, m in enumerate(messages, 1):
            if m.role == "system":
                continue
            prev = (m.content or "").strip().splitlines()[0] if m.content else ""
            print(f"  {idx:2}. [{m.role:9}] {prev[:60]}")
        print()
