"""Slash command dispatcher and registry for CoderAI Shell.

Models CoderAI CLI's shell-level slash commands with pure-terminal execution,
structured registration via SlashCommandRegistry, and typed ShellContext.
"""

from __future__ import annotations

from collections.abc import Coroutine
from enum import Enum, auto
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any

from rich.panel import Panel
from rich.table import Table

from coderai.utils.slashcmd import SlashCommandRegistry


class SlashAction(Enum):
    """Action to take after dispatching a slash command."""
    HANDLED = auto()   # Command handled, continue interactive REPL loop
    EXIT = auto()      # Exit application
    TURN = auto()      # Execute prompt from ctx.turn_prompt as an agent turn


@dataclass
class ShellContext:
    """Encapsulates interactive shell state during slash command execution."""
    mgr: Any
    session_id: str | None
    active_plan_mode: bool
    console: Any
    yes: bool
    pending_skills: list[str] = field(default_factory=list)
    ptk_session: Any = None
    turn_prompt: str | None = None
    thinking_expanded: bool = False


# Registries
registry: SlashCommandRegistry[Any] = SlashCommandRegistry()
shell_mode_registry: SlashCommandRegistry[Any] = SlashCommandRegistry()


def _queue_skill(
    mgr: Any,
    ui_console: Any,
    name: str,
    pending_skills: list[str],
    *,
    quiet_unknown: bool = False,
) -> bool:
    from coderai.skill import load_skill

    if not name.strip():
        print("Usage: /skill <name>")
        return False
    skill = load_skill(name, mgr.project_root)
    if not skill:
        if not quiet_unknown:
            print(f"Unknown skill: {name}")
        return False
    if skill["name"] not in pending_skills:
        pending_skills.append(skill["name"])
    loaded_msg = f"Skill '{skill['name']}' ready."
    if ui_console is not None:
        try:
            ui_console.print(f"[bold green]{loaded_msg}[/]")
        except Exception:
            print(loaded_msg)
    else:
        print(loaded_msg)
    return True


# --- Command Registrations ---

@registry.command(aliases=["quit"])
@shell_mode_registry.command(aliases=["quit"])
async def cmd_exit(ctx: ShellContext, args: str) -> SlashAction:
    """Exit the application."""
    return SlashAction.EXIT


@registry.command(aliases=["h", "?"])
def cmd_help(ctx: ShellContext, args: str) -> SlashAction:
    """Display slash command cheatsheet or contextual command help."""
    from coderai.ui.shell.slash import render_help

    render_help(args.strip() if args.strip() else None, ctx.console)
    return SlashAction.HANDLED


@registry.command
def cmd_clear(ctx: ShellContext, args: str) -> SlashAction:
    """Clear the terminal screen."""
    try:
        if ctx.console is not None and hasattr(ctx.console, "clear"):
            ctx.console.clear()
        else:
            import sys

            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
    except Exception:
        try:
            print("\n" * 40)
        except Exception:
            pass
    return SlashAction.HANDLED


@registry.command(aliases=["auth", "keys", "configure"])
def cmd_setup(ctx: ShellContext, args: str) -> SlashAction:
    """Run interactive setup wizard for providers, keys, and models."""
    from coderai.ui.shell.setup import run_setup_wizard

    run_setup_wizard(
        ctx.console,
        project_root=ctx.mgr.project_root,
        mgr=ctx.mgr,
        initial_subcommand=args.strip() if args.strip() else None,
    )
    return SlashAction.HANDLED


@registry.command
def cmd_doctor(ctx: ShellContext, args: str) -> SlashAction:
    """Run system and connectivity diagnostics."""
    from coderai.cli.doctor import render_doctor, run_doctor_diagnostics

    report = run_doctor_diagnostics(ctx.mgr.project_root, ctx.mgr)
    render_doctor(ctx.console, report)
    return SlashAction.HANDLED


@registry.command(aliases=["job"])
def cmd_jobs(ctx: ShellContext, args: str) -> SlashAction:
    """Inspect and manage background jobs."""
    job_store = getattr(ctx.mgr, "job_store", None)
    if not job_store:
        print("Job store subsystem is not initialized.")
        return SlashAction.HANDLED
    tokens_sub = args.split(None, 1)
    sub_action = tokens_sub[0].lower() if tokens_sub else "list"
    job_target = tokens_sub[1].strip() if len(tokens_sub) > 1 else ""
    if sub_action in ("", "list"):
        jobs = [
            j
            for j in getattr(job_store, "_jobs", {}).values()
            if not ctx.session_id
            or j.session_id == ctx.session_id
            or j.session_id == "default"
        ]
        if not jobs:
            print("No background jobs recorded in active session.")
        else:
            if ctx.console is not None:
                jt = Table(title="Session Background Jobs", border_style="cyan")
                jt.add_column("Status", width=12)
                jt.add_column("Job ID", style="bold cyan", width=14)
                jt.add_column("Kind", style="magenta", width=10)
                jt.add_column("Command / Label", style="white")
                for j in jobs:
                    status_color = (
                        "green"
                        if j.status == "completed"
                        else ("yellow" if j.status == "running" else "red")
                    )
                    jt.add_row(f"[{status_color}]{j.status.upper()}[/]", j.id, j.kind, j.label[:60])
                ctx.console.print(jt)
            else:
                for j in jobs:
                    print(f"[{j.status.upper():9}] {j.id} ({j.kind}) {j.label}")
    elif sub_action == "kill" and job_target:
        ok = job_store.kill_job(job_target)
        print(f"✓ Terminated job {job_target}" if ok else f"Failed to kill job '{job_target}'.")
    elif sub_action == "logs" and job_target:
        job = job_store.get_job(job_target)
        if not job or not job.log_path:
            print(f"No logs found for job '{job_target}'.")
        else:
            p = pathlib.Path(job.log_path)
            if p.exists():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                print("\n".join(lines) or "(log file empty)")
            else:
                print(f"Log file {job.log_path} does not exist.")
    else:
        print("Usage: /jobs [list|kill <id>|logs <id>]")
    return SlashAction.HANDLED


@registry.command
def cmd_schedule(ctx: ShellContext, args: str) -> SlashAction:
    """Manage reminders and background timers."""
    sched_mgr = getattr(ctx.mgr, "schedule_manager", None)
    if not sched_mgr:
        print("Schedule manager subsystem is not initialized.")
        return SlashAction.HANDLED
    tokens_sub = args.split(None, 2)
    sched_action = tokens_sub[0].lower() if tokens_sub else "list"
    if sched_action in ("", "list"):
        records = list(getattr(sched_mgr, "_schedules", {}).values())
        if not records:
            print("No scheduled timers or reminders in workspace.")
        else:
            if ctx.console is not None:
                st = Table(title="Scheduled Timers & Reminders", border_style="cyan")
                st.add_column("ID", style="bold cyan", width=6)
                st.add_column("State", width=12)
                st.add_column("Kind", style="magenta", width=8)
                st.add_column("Scheduled At", style="dim", width=22)
                st.add_column("Prompt / Instruction", style="white")
                for r in records:
                    state_color = "green" if r.state == "dispatched" else ("yellow" if r.state == "scheduled" else "dim")
                    st.add_row(r.id, f"[{state_color}]{r.state}[/]", r.kind, r.scheduled_at[:19], r.prompt[:50])
                ctx.console.print(st)
            else:
                for r in records:
                    print(f"[{r.state:10}] ID:{r.id:4} Kind:{r.kind:6} At:{r.scheduled_at[:19]} -> {r.prompt[:50]}")
    elif sched_action == "after" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit():
        sec = int(tokens_sub[1])
        sched_prompt = tokens_sub[2]
        rec = sched_mgr.create(prompt=sched_prompt, after_seconds=sec, session_id=ctx.session_id)
        print(f"✓ Scheduled reminder #{rec.id} in {sec}s: {sched_prompt}")
    elif sched_action == "every" and len(tokens_sub) >= 3 and tokens_sub[1].isdigit():
        sec = int(tokens_sub[1])
        sched_prompt = tokens_sub[2]
        rec = sched_mgr.create(prompt=sched_prompt, every_seconds=sec, session_id=ctx.session_id)
        print(f"✓ Scheduled recurring reminder #{rec.id} every {sec}s: {sched_prompt}")
    elif sched_action == "at" and len(tokens_sub) >= 3:
        at_arg = tokens_sub[1]
        sched_prompt = tokens_sub[2]
        try:
            rec = sched_mgr.create(prompt=sched_prompt, at=at_arg, session_id=ctx.session_id)
        except ValueError as exc:
            print(f"Invalid schedule time: {exc}")
        else:
            print(f"✓ Scheduled reminder #{rec.id} at {rec.scheduled_at}: {sched_prompt}")
    elif sched_action in ("cancel", "rm", "delete") and len(tokens_sub) >= 2:
        cid = tokens_sub[1]
        res = sched_mgr.delete(cid)
        print(f"✓ Cancelled schedule #{cid}" if res else f"Schedule #{cid} not found.")
    else:
        print("Usage: /schedule [list|after <sec> <prompt>|at <ISO-time> <prompt>|every <sec> <prompt>|cancel <id>]")
    return SlashAction.HANDLED


@registry.command(name="agent", aliases=["role", "roles"])
def cmd_agent(ctx: ShellContext, args: str) -> SlashAction:
    """View or switch active agent role."""
    arg_clean = args.strip()
    if not arg_clean:
        from coderai.ui.shell.session_picker import select_agent_role_interactive

        current_role = ctx.mgr.get_active_agent_role() if hasattr(ctx.mgr, "get_active_agent_role") else "default"
        chosen_role = select_agent_role_interactive(ctx.console, current_role, ctx.mgr.project_root)
        if chosen_role and chosen_role != current_role:
            if hasattr(ctx.mgr, "switch_agent_role") and ctx.mgr.switch_agent_role(chosen_role):
                if ctx.console is not None:
                    ctx.console.print(f"[bold green]✓ Switched active agent role to '[bold white]{chosen_role}[/bold white]'.[/]")
                else:
                    print(f"✓ Switched active agent role to '{chosen_role}'.")
            else:
                print(f"Failed to switch agent role to '{chosen_role}'.")
        return SlashAction.HANDLED
    elif arg_clean.lower() in ("roles", "list", "specs"):
        from pathlib import Path
        from coderai.subagents.registry import discover_markdown_agents

        project_root = Path(getattr(ctx.mgr, "project_root", "."))
        discovered = discover_markdown_agents(project_root)
        bundled = ["default", "okabe"]
        cur_role = ctx.mgr.get_active_agent_role() if hasattr(ctx.mgr, "get_active_agent_role") else "default"
        if ctx.console is not None:
            rt = Table(title="Available Agent Roles & Specifications", border_style="cyan")
            rt.add_column("Role / Spec", style="bold cyan", width=22)
            rt.add_column("Type", style="magenta", width=12)
            rt.add_column("Mode", width=10)
            rt.add_column("Active", width=8)
            rt.add_column("Description", style="white")
            for b in bundled:
                active_mark = "[bold green]● YES[/]" if b == cur_role else "[dim]○[/]"
                rt.add_row(b, "bundled", "primary", active_mark, f"Bundled {b} agent specification")
            for d in discovered:
                active_mark = "[bold green]● YES[/]" if d.name == cur_role else "[dim]○[/]"
                rt.add_row(d.name, "discovered", d.mode, active_mark, d.description)
            ctx.console.print(rt)
        else:
            print(f"Active role: {cur_role}")
            print("Bundled agent specs:")
            for b in bundled:
                print(f"  • {b} {'(active)' if b == cur_role else ''}")
            print("\nDiscovered role specifications (.coderai/agents/*.md):")
            for d in discovered:
                print(f"  • {d.name} [{d.mode}] - {d.description} {'(active)' if d.name == cur_role else ''}")
        return SlashAction.HANDLED
    else:
        target_role = arg_clean.lower()
        if hasattr(ctx.mgr, "switch_agent_role") and ctx.mgr.switch_agent_role(target_role):
            if ctx.console is not None:
                ctx.console.print(f"[bold green]✓ Switched active agent role to '[bold white]{target_role}[/bold white]'.[/]")
            else:
                print(f"✓ Switched active agent role to '{target_role}'.")
        else:
            print(f"Failed to switch to agent role '{target_role}'. Use '/agent roles' to see available specs.")
        return SlashAction.HANDLED


@registry.command(name="agents", aliases=["subagents", "subagent"])
def cmd_agents(ctx: ShellContext, args: str) -> SlashAction:
    """Inspect subagent runs and discovered roles."""
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "list"
    if sub in ("roles", "list", "specs"):
        return cmd_agent(ctx, "roles")
    if sub in ("tree", "report", "send"):
        print(
            f"/agents {sub} is not yet implemented in this CLI. "
            "Use '/agents roles' to list roles or '/agent roles' to switch."
        )
        return SlashAction.HANDLED
    print(
        "No live subagent run registry in this session. "
        "Use '/agents roles' to list available roles."
    )
    return SlashAction.HANDLED


@registry.command(name="title", aliases=["rename"])
def cmd_title_action(ctx: ShellContext, args: str) -> SlashAction:
    """View or set the session title."""
    from coderai.ui.shell.slash import cmd_title

    cmd_title(ctx.mgr, ctx.session_id, args, ctx.console)
    return SlashAction.HANDLED


@registry.command(aliases=["edit"])
def cmd_editor(ctx: ShellContext, args: str) -> SlashAction:
    """Compose a prompt in external $EDITOR."""
    from coderai.utils.editor import open_external_editor

    initial_draft = args.strip() if args.strip() else ""
    composed = open_external_editor(initial_draft)
    if not composed:
        print("Editor closed with empty content; prompt cancelled.")
        return SlashAction.HANDLED
    if ctx.console is not None:
        ctx.console.print(f"[bold cyan]Prompt submitted from editor ({len(composed)} chars)[/]")
    else:
        print(f"Prompt submitted from editor ({len(composed)} chars)")
    ctx.turn_prompt = composed
    return SlashAction.TURN


@registry.command
def cmd_paste(ctx: ShellContext, args: str) -> SlashAction:
    """Enter multiline paste mode."""
    from coderai.ui.shell.prompt import read_paste_mode

    composed = read_paste_mode()
    if not composed:
        print("Paste buffer empty; prompt cancelled.")
        return SlashAction.HANDLED
    ctx.turn_prompt = composed
    return SlashAction.TURN


@registry.command(aliases=["cost"])
def cmd_tokens(ctx: ShellContext, args: str) -> SlashAction:
    """Show token usage breakdown."""
    from coderai.ui.shell.session_picker import render_token_breakdown

    render_token_breakdown(ctx.console, ctx.mgr, ctx.session_id)
    return SlashAction.HANDLED


@registry.command
def cmd_context(ctx: ShellContext, args: str) -> SlashAction:
    """Inspect live context window utilization."""
    entry = ctx.mgr.get_session(ctx.session_id) if ctx.session_id else None
    if not entry:
        print("No active session.")
        return SlashAction.HANDLED
    from coderai.config import get_default_context_window

    active_tokens = entry.active_tokens
    model = ctx.mgr.get_active_model()
    max_ctx = get_default_context_window(model)
    pct = (active_tokens / max_ctx * 100) if max_ctx > 0 else 0
    bar = "■" * int(pct / 10) + "□" * (10 - int(pct / 10))
    if ctx.console is not None:
        t = Table.grid(padding=(0, 2))
        t.add_column("Key", style="dim cyan", width=18)
        t.add_column("Value", style="bold white")
        t.add_row("Model:", model)
        t.add_row("Active tokens:", f"{active_tokens:,} / {max_ctx:,} ({pct:.1f}%)")
        t.add_row("Usage bar:", f"[{bar}] {pct:.0f}%")
        t.add_row("Session ID:", ctx.session_id[:12] if ctx.session_id else "none")
        ctx.console.print(Panel(t, title="[bold cyan]Context Window[/]", border_style="blue"))
    else:
        print(f"Context: {active_tokens:,} / {max_ctx:,} ({pct:.1f}%) [{bar}]")
    return SlashAction.HANDLED


@registry.command(aliases=["settings"])
def cmd_config(ctx: ShellContext, args: str) -> SlashAction:
    """Show resolved configuration."""
    from coderai.ui.shell.session_picker import render_config_interactive

    render_config_interactive(ctx.console, ctx.mgr.project_root)
    return SlashAction.HANDLED


@registry.command(aliases=["permissions"])
def cmd_permission(ctx: ShellContext, args: str) -> SlashAction:
    """Show or set the permission preset."""
    from coderai.sandbox import SANDBOX_MODES, parse_sandbox_mode, preset_permissions
    from coderai.config import read_project_settings, write_project_settings

    arg = args.strip().lower()
    if not arg:
        perms = ctx.mgr.get_resolved_settings().get("permissions") or {}
        msg = (
            f"Permission preset: {perms.get('preset') or 'unset (danger-full-access default)'}\n"
            f"Sandbox: {perms.get('sandbox')}\n"
            f"allow={perms.get('allow')}\n"
            f"deny={perms.get('deny')}\n"
            f"ask={perms.get('ask')}\n"
            f"Usage: /permission {' | '.join(SANDBOX_MODES)}"
        )
        print(msg)
        return SlashAction.HANDLED
    parsed = parse_sandbox_mode(arg)
    if not parsed:
        print(f"Unknown preset '{args}'. Use: {', '.join(SANDBOX_MODES)}")
        return SlashAction.HANDLED
    settings = read_project_settings(ctx.mgr.project_root) or {}
    permissions = dict(settings.get("permissions") or {})
    mapped = preset_permissions(parsed)
    permissions.update({
        "preset": parsed,
        "allow": mapped["allow"],
        "deny": mapped["deny"],
        "ask": mapped["ask"],
        "defaultMode": mapped["defaultMode"],
    })
    settings["permissions"] = permissions
    write_project_settings(settings, ctx.mgr.project_root)
    print(f"Permission preset set to {parsed}. New sessions will use this preset.")
    return SlashAction.HANDLED


@registry.command
def cmd_goal(ctx: ShellContext, args: str) -> SlashAction:
    """List or update session goals."""
    from coderai.goals.core import get_goal_store

    store = get_goal_store(ctx.mgr.project_root)
    sid = ctx.session_id or "default"
    tokens = args.split(None, 1)
    goal_action = tokens[0].lower() if tokens else "list"
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    if goal_action in ("", "list"):
        print(store.format(sid))
    elif goal_action == "add" and rest:
        goal = store.add(sid, rest)
        print(f"Added goal {goal.id}: {goal.objective}")
    elif goal_action in ("done", "cancel", "start") and rest:
        status_map = {"done": "done", "cancel": "cancelled", "start": "in_progress"}
        updated = store.update(sid, rest, status=status_map[goal_action])
        print(f"Updated {updated.id}" if updated else f"Unknown goal '{rest}'")
    else:
        print("Usage: /goal [list|add <title>|done <id>|cancel <id>]")
    return SlashAction.HANDLED


@registry.command
def cmd_history(ctx: ShellContext, args: str) -> SlashAction:
    """Show the session timeline."""
    from coderai.ui.shell.session_picker import render_session_history

    render_session_history(ctx.console, ctx.mgr, ctx.session_id)
    return SlashAction.HANDLED


@registry.command
async def cmd_mcp(ctx: ShellContext, args: str) -> SlashAction:
    """Inspect MCP servers, tools, prompts, and resources."""
    from coderai.ui.shell.session_picker import (
        render_mcp_interactive,
        render_mcp_prompts,
        render_mcp_resources_async,
    )

    if args.startswith("reconnect"):
        server_name = args.replace("reconnect", "", 1).strip()
        if not server_name:
            print("Usage: /mcp reconnect <server_name>")
            return SlashAction.HANDLED
        reconnected = await ctx.mgr.mcp_manager.reconnect(server_name)
        ctx.mgr._refresh_mcp_tool_definitions()
        if reconnected:
            if ctx.console is not None:
                ctx.console.print(f"[bold green]✓ Reconnected MCP server '[cyan]{server_name}[/]'.[/]")
            else:
                print(f"✓ Reconnected MCP server '{server_name}'.")
        else:
            status = next((s for s in ctx.mgr.mcp_manager.server_statuses if s.name == server_name), None)
            err_msg = f": {status.error}" if status and status.error else ""
            if ctx.console is not None:
                ctx.console.print(f"[bold red]Failed to reconnect MCP server '{server_name}'{err_msg}[/]")
            else:
                print(f"Failed to reconnect MCP server '{server_name}'{err_msg}")
    elif args.startswith("prompts"):
        render_mcp_prompts(ctx.console, ctx.mgr)
    elif args.startswith("resources"):
        uri_arg = args.replace("resources", "", 1).strip() or None
        await render_mcp_resources_async(ctx.console, ctx.mgr, uri=uri_arg)
    else:
        render_mcp_interactive(ctx.console, ctx.mgr)
    return SlashAction.HANDLED


@registry.command
def cmd_theme(ctx: ShellContext, args: str) -> SlashAction:
    """Switch theme dark/light."""
    arg = args.strip().lower()
    if arg in ("dark", "light"):
        try:
            from coderai.ui.theme import set_active_theme

            set_active_theme(arg)  # type: ignore[arg-type]
            if ctx.console is not None:
                ctx.console.print(f"[bold green]Theme set to {arg}[/]")
            else:
                print(f"Theme set to {arg}")
        except Exception as e:
            print(f"Failed to set theme: {e}")
    elif not arg:
        try:
            from coderai.ui.theme import get_active_theme

            cur = get_active_theme()
            if ctx.console is not None:
                ctx.console.print(f"[bold cyan]Current theme:[/] {cur} (use /theme dark|light)")
            else:
                print(f"Current theme: {cur}")
        except Exception:
            print("Theme: dark (default)")
    else:
        print("Usage: /theme [dark|light]")
    return SlashAction.HANDLED


@registry.command(aliases=["raw"])
def cmd_thinking(ctx: ShellContext, args: str) -> SlashAction:
    """Toggle reasoning trace display."""
    arg = args.strip().lower()
    if arg in ("full", "on", "normal"):
        ctx.thinking_expanded = True
        msg = "Full expanded view enabled."
    elif arg in ("summary", "off", "lite"):
        ctx.thinking_expanded = False
        msg = "Concise summary view enabled."
    else:
        ctx.thinking_expanded = not ctx.thinking_expanded
        mode_str = "Full expanded" if ctx.thinking_expanded else "Concise summary"
        msg = f"Switched to {mode_str}."
    if ctx.console is not None:
        ctx.console.print(f"[bold magenta]Reasoning traces:[/] {msg}")
    else:
        print(f"Reasoning traces: {msg}")
    return SlashAction.HANDLED


@registry.command(name="export")
def cmd_export_action(ctx: ShellContext, args: str) -> SlashAction:
    """Export session history to Markdown or JSON."""
    from coderai.utils.export import export_session_to_json, export_session_to_markdown

    if not ctx.session_id:
        print("No active session to export.")
        return SlashAction.HANDLED
    clean_arg = args.strip()
    try:
        if clean_arg.endswith(".json"):
            exported_file = export_session_to_json(ctx.mgr, ctx.session_id, clean_arg)
        else:
            exported_file = export_session_to_markdown(ctx.mgr, ctx.session_id, clean_arg if clean_arg else None)
    except Exception as exc:
        print(f"Export failed: {exc}")
        return SlashAction.HANDLED
    if ctx.console is not None:
        ctx.console.print(f"[bold green]✓ Session successfully exported to:[/] [cyan]{exported_file}[/]")
    else:
        print(f"✓ Session successfully exported to: {exported_file}")
    return SlashAction.HANDLED


@registry.command
def cmd_fork(ctx: ShellContext, args: str) -> SlashAction:
    """Fork the current or specified session."""
    target_to_fork = args.strip() if args.strip() else ctx.session_id
    if not target_to_fork:
        print("No active session to fork. Usage: /fork <session_id>")
        return SlashAction.HANDLED
    forked_id = ctx.mgr.fork_session(target_to_fork)
    if forked_id:
        ctx.session_id = forked_id
        resumed_entry = ctx.mgr.get_session(ctx.session_id)
        if resumed_entry:
            ctx.active_plan_mode = resumed_entry.plan_mode
        if ctx.console is not None:
            ctx.console.print(f"[bold green]✓ Forked and switched to session:[/] {ctx.session_id}")
        else:
            print(f"Forked and switched to session: {ctx.session_id}")
    else:
        print(f"Failed to fork session '{target_to_fork}'.")
    return SlashAction.HANDLED


@registry.command(aliases=["rm"])
def cmd_delete(ctx: ShellContext, args: str) -> SlashAction:
    """Delete a saved session."""
    del_target_id = args.strip() if args.strip() else ctx.session_id
    if not del_target_id:
        print("Usage: /delete <session_id>")
        return SlashAction.HANDLED
    if ctx.mgr.delete_session(del_target_id):
        if ctx.session_id == del_target_id:
            ctx.session_id = None
        if ctx.console is not None:
            ctx.console.print(f"[bold green]✓ Deleted session:[/] [red]{del_target_id}[/]")
        else:
            print(f"✓ Deleted session: {del_target_id}")
    else:
        print(f"No saved session with id '{del_target_id}'.")
    return SlashAction.HANDLED


@registry.command
async def cmd_compact(ctx: ShellContext, args: str) -> SlashAction:
    """Compress conversation context."""
    if not ctx.session_id:
        print("No active session to compact.")
        return SlashAction.HANDLED
    t0 = time.time()
    custom = args.strip() if args.strip() else None
    await ctx.mgr.compact_session(ctx.session_id, trigger="manual", custom_instruction=custom)
    elapsed = time.time() - t0
    entry = ctx.mgr.get_session(ctx.session_id)
    active_tokens = entry.active_tokens if entry else 0
    if ctx.console is not None:
        ctx.console.print(
            f"[bold green]✓ Session context compacted in {elapsed:.1f}s.[/] [dim]Active tokens: {active_tokens:,}[/]"
        )
    else:
        print(f"✓ Session context compacted in {elapsed:.1f}s. Active tokens: {active_tokens:,}")
    return SlashAction.HANDLED


@registry.command(name="continue")
async def cmd_continue(ctx: ShellContext, args: str, drain_fn: Any = None) -> SlashAction:
    """Continue agent execution."""
    if not ctx.session_id:
        print("No active session to continue.")
        return SlashAction.HANDLED
    await ctx.mgr.reply_session(ctx.session_id, "/continue")
    if callable(drain_fn):
        await drain_fn(ctx.mgr, ctx.session_id, ctx.yes)
    return SlashAction.HANDLED


@registry.command
def cmd_plan(ctx: ShellContext, args: str) -> SlashAction:
    """Toggle or apply Plan Mode."""
    sub = args.strip().lower()
    if sub == "on":
        ctx.active_plan_mode = True
    elif sub == "off":
        ctx.active_plan_mode = False
    elif sub in ("view", "show"):
        plan_content = ctx.mgr.read_plan(ctx.session_id) if ctx.session_id else None
        if plan_content:
            if ctx.console is not None:
                from rich.markdown import Markdown

                ctx.console.print(Markdown(plan_content))
            else:
                print(plan_content)
        else:
            print("No plan recorded for this session. Use /plan on to start planning.")
        return SlashAction.HANDLED
    elif sub in ("clear", "reset"):
        if ctx.session_id:
            ctx.mgr.clear_plan(ctx.session_id)
        print("Session plan cleared.")
        return SlashAction.HANDLED
    elif sub == "apply":
        plan_content = ctx.mgr.read_plan(ctx.session_id) if ctx.session_id else None
        if not plan_content:
            print("No plan recorded to apply.")
            return SlashAction.HANDLED
        ctx.active_plan_mode = False
        if ctx.ptk_session is not None and hasattr(ctx.ptk_session, "set_plan_mode"):
            ctx.ptk_session.set_plan_mode(False)
        print("Exited Plan Mode. Executing plan...")
        ctx.turn_prompt = f"Implement the following plan step by step:\n\n{plan_content}"
        return SlashAction.TURN
    else:
        ctx.active_plan_mode = not ctx.active_plan_mode

    if ctx.ptk_session is not None and hasattr(ctx.ptk_session, "set_plan_mode"):
        ctx.ptk_session.set_plan_mode(ctx.active_plan_mode)

    plan_file = ctx.mgr.get_plan_path(ctx.session_id) if ctx.session_id else "PLAN.md"
    if ctx.active_plan_mode:
        if ctx.console is not None:
            ctx.console.print(
                f"[bold yellow]Plan Mode ON.[/] Write your plan to [cyan]{plan_file}[/cyan].\n"
                f"[dim]Tools: Read, Search, Glob, Web (no code modifications permitted).[/dim]\n"
                f"[dim]Use [bold]/plan apply[/bold] to execute, or [bold]/plan off[/bold] to exit manually.[/dim]"
            )
        else:
            print(f"Plan Mode ON. Write your plan to {plan_file}. Use /plan apply to execute.")
    else:
        if ctx.console is not None:
            ctx.console.print(
                "[bold green]Plan Mode OFF.[/] [dim]All standard editing tools are active.[/dim]"
            )
        else:
            print("Plan Mode OFF. All tools active.")
    return SlashAction.HANDLED


@registry.command
def cmd_diff(ctx: ShellContext, args: str) -> SlashAction:
    """Show the current unified diff."""
    from coderai.utils.rich.diff_render import render_diff_preview

    diff_output = ""
    if ctx.session_id and hasattr(ctx.mgr, "get_diff"):
        try:
            diff_output = ctx.mgr.get_diff(ctx.session_id) or ""
        except Exception:
            diff_output = ""
    if not str(diff_output).strip():
        if ctx.console is not None:
            ctx.console.print("[dim]No file changes detected since session start.[/]")
        else:
            print("No file changes detected since session start.")
        return SlashAction.HANDLED
    render_diff_preview(ctx.console, diff_output, title="Session File Diffs")
    return SlashAction.HANDLED


@registry.command
def cmd_model(ctx: ShellContext, args: str) -> SlashAction:
    """Select or switch the active model."""
    arg_clean = args.strip()
    if not arg_clean:
        from coderai.ui.shell.session_picker import select_model_interactive

        chosen = select_model_interactive(ctx.console, ctx.mgr.get_active_model())
        if chosen and chosen != ctx.mgr.get_active_model():
            try:
                ctx.mgr.set_model(chosen)
            except ValueError as exc:
                print(f"Invalid model: {exc}")
                return SlashAction.HANDLED
            if ctx.console is not None:
                ctx.console.print(f"[bold green]✓ Switched active model to:[/] [cyan]{chosen}[/]")
            else:
                print(f"✓ Switched active model to: {chosen}")
    else:
        try:
            ctx.mgr.set_model(arg_clean)
        except ValueError as exc:
            print(f"Invalid model: {exc}")
            return SlashAction.HANDLED
        if ctx.console is not None:
            ctx.console.print(f"[bold green]✓ Switched active model to:[/] [cyan]{arg_clean}[/]")
        else:
            print(f"✓ Switched active model to: {arg_clean}")
    return SlashAction.HANDLED


@registry.command(aliases=["reasoning"])
def cmd_effort(ctx: ShellContext, args: str) -> SlashAction:
    """Select reasoning effort."""
    valid_efforts = ("max", "high", "medium", "low", "off")
    chosen = args.strip().lower()
    if not chosen:
        from coderai.ui.shell.session_picker import select_reasoning_effort_interactive

        cur = ctx.mgr.get_reasoning_effort() or "max"
        try:
            model = ctx.mgr.get_active_model() if hasattr(ctx.mgr, "get_active_model") else ""
        except Exception:
            model = ""
        chosen = select_reasoning_effort_interactive(ctx.console, cur, model or "")
    if chosen:
        if chosen in valid_efforts:
            ctx.mgr.set_reasoning_effort(chosen)
            if ctx.console is not None:
                ctx.console.print(f"[bold green]✓ Reasoning effort set to:[/] [cyan]{chosen}[/]")
            else:
                print(f"✓ Reasoning effort set to: {chosen}")
        else:
            print(f"Invalid effort level '{chosen}'. Valid: {', '.join(valid_efforts)}")
    return SlashAction.HANDLED


@registry.command(name="sessions", aliases=["resume"])
def cmd_sessions(ctx: ShellContext, args: str) -> SlashAction:
    """Browse, resume, delete, or fork sessions."""
    from coderai.ui.shell.session_picker import select_session_interactive

    arg_clean = args.strip()
    if arg_clean:
        resolved = ctx.mgr.resolve_session_id(arg_clean)
        if resolved:
            ctx.session_id = resolved
            entry = ctx.mgr.get_session(ctx.session_id)
            if entry:
                ctx.active_plan_mode = entry.plan_mode
            print(f"✓ Resumed session: {ctx.session_id}")
        else:
            print(f"No saved session matching '{arg_clean}'.")
        return SlashAction.HANDLED

    sessions = ctx.mgr.list_sessions()[:25]
    if not sessions:
        print("No saved sessions found in this workspace.")
        return SlashAction.HANDLED
    action = select_session_interactive(ctx.console, sessions)
    if action:
        if action.startswith("fork:"):
            target = action.split(":", 1)[1]
            forked = ctx.mgr.fork_session(target)
            if forked:
                ctx.session_id = forked
                print(f"✓ Forked session: {ctx.session_id}")
        elif action.startswith("delete:"):
            target = action.split(":", 1)[1]
            ctx.mgr.delete_session(target)
            if ctx.session_id == target:
                ctx.session_id = None
            print(f"✓ Deleted session: {target}")
        else:
            ctx.session_id = action
            entry = ctx.mgr.get_session(ctx.session_id)
            if entry:
                ctx.active_plan_mode = entry.plan_mode
            print(f"✓ Resumed session: {ctx.session_id}")
    return SlashAction.HANDLED


@registry.command
def cmd_skills(ctx: ShellContext, args: str) -> SlashAction:
    """Browse discovered skills."""
    from coderai.ui.shell.session_picker import render_skills_interactive

    render_skills_interactive(ctx.console, ctx.mgr.project_root)
    return SlashAction.HANDLED


@registry.command
def cmd_skill(ctx: ShellContext, args: str) -> SlashAction:
    """Load a skill into this session."""
    skill_name = args.strip()
    if _queue_skill(ctx.mgr, ctx.console, skill_name, ctx.pending_skills):
        if ctx.session_id:
            ctx.mgr.inject_skills(ctx.session_id, ctx.pending_skills)
            ctx.pending_skills.clear()
    return SlashAction.HANDLED


@registry.command
def cmd_undo(ctx: ShellContext, args: str) -> SlashAction:
    """Revert to a previous checkpoint."""
    from coderai.ui.shell.session_picker import select_undo_interactive

    if not ctx.session_id:
        print("No active session to undo.")
        return SlashAction.HANDLED
    try:
        targets = ctx.mgr.list_undo_targets(ctx.session_id)
    except Exception as exc:
        print(f"Undo unavailable: {exc}")
        return SlashAction.HANDLED
    if not targets:
        print("No undoable turns in this session.")
        return SlashAction.HANDLED
    chosen, mode = select_undo_interactive(ctx.console, targets)
    if chosen is None:
        print("Undo cancelled.")
        return SlashAction.HANDLED
    try:
        ok = ctx.mgr.undo(
            ctx.session_id,
            target_message_id=(chosen.get("message_id") if isinstance(chosen, dict) else None),
            mode=mode,
        )
    except Exception as exc:
        print(f"Undo failed: {exc}")
        return SlashAction.HANDLED
    if ok:
        print(f"✓ Restored checkpoint for turn #{chosen.get('index', '?')} ({mode}).")
    else:
        print("Undo failed: checkpoint unavailable or already at target.")
    return SlashAction.HANDLED


@registry.command(name="new")
def cmd_new(ctx: ShellContext, args: str) -> SlashAction:
    """Start a fresh session."""
    ctx.session_id = None
    ctx.active_plan_mode = False
    if ctx.ptk_session is not None and hasattr(ctx.ptk_session, "set_plan_mode"):
        ctx.ptk_session.set_plan_mode(False)
    if ctx.console is not None:
        ctx.console.print("[bold green]✓ Started new session context.[/]")
    else:
        print("✓ Started new session context.")
    return SlashAction.HANDLED


@registry.command
async def cmd_init(ctx: ShellContext, args: str) -> SlashAction:
    """Initialize or update AGENTS.md guidelines."""
    from coderai.soul.agent import load_agents_md

    await load_agents_md(pathlib.Path(ctx.mgr.project_root))
    if ctx.console is not None:
        ctx.console.print("[bold green]✓ Initialized AGENTS.md guidelines.[/]")
    else:
        print("✓ Initialized AGENTS.md guidelines.")
    return SlashAction.HANDLED


def _sync_auto_approve_context(ctx: ShellContext) -> None:
    """Keep ShellContext.yes aligned with the manager's live YOLO/AFK flags."""
    mgr = ctx.mgr
    if hasattr(mgr, "is_auto_approve"):
        ctx.yes = bool(mgr.is_auto_approve())
    else:
        ctx.yes = bool(getattr(mgr, "yolo", False) or getattr(mgr, "afk", False))


@registry.command
def cmd_yolo(ctx: ShellContext, args: str) -> SlashAction:
    """Toggle YOLO auto-approve all actions."""
    mgr = ctx.mgr
    if hasattr(mgr, "set_yolo") and hasattr(mgr, "is_yolo"):
        mgr.set_yolo(not bool(mgr.is_yolo()))
        enabled = bool(mgr.is_yolo())
    else:
        enabled = not bool(getattr(mgr, "yolo", False))
        mgr.yolo = enabled
    _sync_auto_approve_context(ctx)
    if enabled:
        if ctx.console is not None:
            ctx.console.print("[bold red]YOLO mode ON.[/] [dim]All actions auto-approved.[/dim]")
        else:
            print("YOLO mode ON. All actions auto-approved.")
    else:
        if ctx.console is not None:
            ctx.console.print("[bold green]YOLO mode OFF.[/] [dim]Approvals will prompt.[/dim]")
        else:
            print("YOLO mode OFF. Approvals will prompt.")
    return SlashAction.HANDLED


@registry.command
def cmd_afk(ctx: ShellContext, args: str) -> SlashAction:
    """Toggle AFK auto-dismiss questions & approvals."""
    mgr = ctx.mgr
    if hasattr(mgr, "set_afk") and hasattr(mgr, "is_afk"):
        mgr.set_afk(not bool(mgr.is_afk()))
        enabled = bool(mgr.is_afk())
    else:
        enabled = not bool(getattr(mgr, "afk", False))
        mgr.afk = enabled
    _sync_auto_approve_context(ctx)
    if enabled:
        if ctx.console is not None:
            ctx.console.print("[bold yellow]AFK mode ON.[/] [dim]Auto-dismiss questions and approvals.[/dim]")
        else:
            print("AFK mode ON. Auto-dismiss questions and approvals.")
    else:
        if ctx.console is not None:
            ctx.console.print("[bold green]AFK mode OFF.[/] [dim]Interactive terminal restored.[/dim]")
        else:
            print("AFK mode OFF. Interactive terminal restored.")
    return SlashAction.HANDLED


@registry.command(name="add-dir", aliases=["add_dir"])
def cmd_add_dir(ctx: ShellContext, args: str) -> SlashAction:
    """Add directory to workspace."""
    arg = args.strip().strip("'\"")
    if not arg:
        dirs = getattr(ctx.mgr, "additional_dirs", [])
        if not dirs:
            print("No additional directories. Usage: /add-dir <path>")
        else:
            print("Additional directories:\n" + "\n".join(f"  - {d}" for d in dirs))
        return SlashAction.HANDLED
    p = pathlib.Path(arg).expanduser().resolve()
    if not p.exists():
        print(f"Directory does not exist: {p}")
        return SlashAction.HANDLED
    if not p.is_dir():
        print(f"Not a directory: {p}")
        return SlashAction.HANDLED
    if str(p) in ctx.mgr.additional_dirs:
        print(f"Directory already in workspace: {p}")
        return SlashAction.HANDLED
    ctx.mgr.additional_dirs.append(str(p))
    print(f"✓ Added directory to workspace: {p}")
    return SlashAction.HANDLED


@registry.command(name="import")
def cmd_import(ctx: ShellContext, args: str) -> SlashAction:
    """Import context from file or session."""
    _MAX_IMPORT_CHARS = 100_000
    target = args.strip().strip("'\"")
    if not target:
        print("Usage: /import <file_path or session_id>")
        return SlashAction.HANDLED
    p = pathlib.Path(target)
    if p.exists() and p.is_file():
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"Failed to read import source {target}: {exc}")
            return SlashAction.HANDLED
        if len(content) > _MAX_IMPORT_CHARS:
            content = content[:_MAX_IMPORT_CHARS] + "\n...[truncated: import capped at 100,000 chars]"
            print(f"Import truncated to {_MAX_IMPORT_CHARS} chars.")
        if not ctx.session_id:
            print(
                f"Read {len(content)} chars from {target} but no active session; "
                "start one with /new first."
            )
            return SlashAction.HANDLED
        try:
            msg = ctx.mgr._build_message(ctx.session_id, "user", f"<system>Imported from {p.name}:\n{content}</system>")
            ctx.mgr._append_message(msg)
        except Exception as exc:
            print(f"Import failed: {exc}")
            return SlashAction.HANDLED
        print(f"✓ Imported file context from {target} ({len(content)} chars).")
    else:
        print(f"Import source not found: {target}")
    return SlashAction.HANDLED


@registry.command
def cmd_reset(ctx: ShellContext, args: str) -> SlashAction:
    """Clear conversation context and reset session state."""
    ctx.session_id = None
    ctx.active_plan_mode = False
    print("✓ Session state reset.")
    return SlashAction.HANDLED


@registry.command
def cmd_version(ctx: ShellContext, args: str) -> SlashAction:
    """Show CLI version."""
    from coderai.ui.shell.slash import cmd_version as _ver

    _ver(ctx.console)
    return SlashAction.HANDLED


@registry.command(aliases=["release-notes"])
def cmd_changelog(ctx: ShellContext, args: str) -> SlashAction:
    """Show recent changelog."""
    from coderai.ui.shell.slash import cmd_changelog as _cl

    _cl(ctx.console)
    return SlashAction.HANDLED


@registry.command
def cmd_feedback(ctx: ShellContext, args: str) -> SlashAction:
    """Submit feedback."""
    from coderai.ui.shell.slash import cmd_feedback as _fb

    _fb(ctx.console, args[:2000] if isinstance(args, str) else args)
    return SlashAction.HANDLED


@registry.command
def cmd_reload(ctx: ShellContext, args: str) -> SlashAction:
    """Reload configuration without exiting."""
    from coderai.ui.shell.slash import cmd_reload as _rl

    _rl(ctx.mgr, ctx.console)
    return SlashAction.HANDLED


@registry.command
def cmd_debug(ctx: ShellContext, args: str) -> SlashAction:
    """Show context debug info (msgs/tokens/checkpoints)."""
    entry = ctx.mgr.get_session(ctx.session_id) if ctx.session_id else None
    if not entry:
        print("No active session.")
        return SlashAction.HANDLED
    msgs = ctx.mgr.list_session_messages(ctx.session_id)
    if ctx.console is not None:
        t = Table(title=f"Session Debug [{ctx.session_id}]", border_style="cyan")
        t.add_column("Property", style="bold cyan")
        t.add_column("Value", style="white")
        t.add_row("Messages", str(len(msgs)))
        t.add_row("Active Tokens", f"{entry.active_tokens:,}")
        t.add_row("Plan Mode", str(entry.plan_mode))
        t.add_row("Model", ctx.mgr.get_active_model())
        ctx.console.print(t)
    else:
        print(f"Session: {ctx.session_id} | Msgs: {len(msgs)} | Active tokens: {entry.active_tokens:,}")
    return SlashAction.HANDLED


@registry.command(aliases=["status", "quota"])
def cmd_usage(ctx: ShellContext, args: str) -> SlashAction:
    """Show API usage / quota."""
    from coderai.ui.shell.session_picker import render_token_breakdown

    render_token_breakdown(ctx.console, ctx.mgr, ctx.session_id)
    return SlashAction.HANDLED


@registry.command
def cmd_login(ctx: ShellContext, args: str) -> SlashAction:
    """Log in / configure API platform."""
    from coderai.ui.shell.slash import cmd_login as _li

    _li(ctx.console, ctx.mgr.project_root, ctx.mgr, args.strip() or None)
    return SlashAction.HANDLED


@registry.command
def cmd_logout(ctx: ShellContext, args: str) -> SlashAction:
    """Log out from current platform."""
    from coderai.ui.shell.slash import cmd_logout as _lo

    _lo(ctx.console, ctx.mgr.project_root, ctx.mgr)
    return SlashAction.HANDLED


@registry.command
def cmd_hooks(ctx: ShellContext, args: str) -> SlashAction:
    """Show configured hooks."""
    from coderai.ui.shell.slash import cmd_hooks as _hk

    _hk(ctx.console, ctx.mgr.project_root)
    return SlashAction.HANDLED


@registry.command
def cmd_upgrade(ctx: ShellContext, args: str) -> SlashAction:
    """Check for and install CoderAI updates."""
    from coderai.ui.shell.slash import cmd_upgrade as _up

    _up(ctx.console)
    return SlashAction.HANDLED


@registry.command
def cmd_task(ctx: ShellContext, args: str) -> SlashAction:
    """Open interactive background-task browser."""
    from coderai.ui.shell.task_browser import run_task_browser

    run_task_browser(ctx.console, ctx.mgr, ctx.session_id)
    return SlashAction.HANDLED


@registry.command(name="web-vis", aliases=["web", "vis", "browser", "web_vis"])
def cmd_web_vis(ctx: ShellContext, args: str) -> SlashAction:
    """Pure CLI notice for web/vis commands."""
    msg = "CoderAI is a pure CLI application. Web UI and browser visualizers have been removed."
    if ctx.console is not None:
        ctx.console.print(f"[yellow]{msg}[/]")
    else:
        print(msg)
    return SlashAction.HANDLED


@registry.command(name="teams", aliases=["team", "swarm"])
def cmd_teams(ctx: ShellContext, args: str) -> SlashAction:
    """Inspect autonomous multi-agent swarm team and task board."""
    from coderai.teams import get_team_manager

    team_mgr = get_team_manager()
    teammates = team_mgr.list_teammates()
    tasks = team_mgr.board.list_tasks()

    if ctx.console is not None:
        tt = Table(title="Autonomous Swarm — Teammates", border_style="cyan")
        tt.add_column("Agent ID", style="bold cyan")
        tt.add_column("Role / Spec", style="magenta")
        tt.add_column("Status", style="green")
        tt.add_column("Turn / Iter", justify="right")
        tt.add_column("Description")
        if not teammates:
            tt.add_row("(none active)", "-", "-", "-", "No autonomous teammates currently spawned.")
        else:
            for tm in teammates:
                tt.add_row(tm.agent_id, tm.role, tm.status, str(tm.turn_count), tm.description or "-")
        ctx.console.print(tt)

        tb = Table(title="Autonomous Swarm — Task Board", border_style="blue")
        tb.add_column("Task ID", style="bold blue")
        tb.add_column("Title", style="white")
        tb.add_column("Assigned", style="cyan")
        tb.add_column("Priority", style="yellow")
        tb.add_column("Status", style="green")
        if not tasks:
            tb.add_row("(no tasks)", "No swarm tasks on board.", "-", "-", "-")
        else:
            for t in tasks:
                tb.add_row(t.task_id, t.title, t.assigned_to or "(unassigned)", t.priority, t.status)
        ctx.console.print(tb)
    else:
        print(f"Autonomous Swarm: {len(teammates)} teammates, {len(tasks)} tasks.")
        for tm in teammates:
            print(f"  • {tm.agent_id} [{tm.role}]: {tm.status}")
        for t in tasks:
            print(f"  [{t.task_id}] {t.title} ({t.status}) -> {t.assigned_to or 'unassigned'}")
    return SlashAction.HANDLED


@registry.command(name="btw", aliases=["side"])
async def cmd_btw(ctx: ShellContext, args: str) -> SlashAction:
    """Ask a lightweight side question without mutating the main conversation history."""
    q = args.strip()
    if not q:
        print("Usage: /btw <question>")
        return SlashAction.HANDLED
    from coderai.soul.btw import run_side_question

    if ctx.console is not None:
        ctx.console.print(f"[dim]Thinking... (side question: {q[:60]}...)[/]")
    try:
        answer = await run_side_question(ctx.mgr, ctx.session_id, q)
        if ctx.console is not None:
            ctx.console.print(Panel(answer, title="[bold cyan]BTW / Side Note[/]", border_style="cyan"))
        else:
            print(f"\n--- BTW ---\n{answer}\n-----------")
    except Exception as err:
        if ctx.console is not None:
            ctx.console.print(f"[red]Side question failed: {err}[/]")
        else:
            print(f"Side question failed: {err}")
    return SlashAction.HANDLED


@registry.command
async def cmd_image(ctx: ShellContext, args: str, drain_fn: Any = None) -> SlashAction:
    """Attach an image for analysis."""
    from coderai.cli.image_attachment import parse_and_attach_image

    tokens_img = args.split(None, 1)
    if not tokens_img:
        print("Usage: /image <file_path> [prompt]")
        return SlashAction.HANDLED
    img_path = tokens_img[0]
    img_prompt = tokens_img[1] if len(tokens_img) > 1 else f"Inspect and analyze image: {img_path}"
    content_param, err = parse_and_attach_image(img_path, ctx.mgr.project_root)
    if err:
        print(f"Image error: {err}")
        return SlashAction.HANDLED
    if content_param is None:
        print("Image error: attachment metadata was not produced")
        return SlashAction.HANDLED
    if ctx.console is not None:
        ctx.console.print(
            f"[bold green]✓ Attached image:[/] [cyan]{content_param['name']}[/] "
            f"({content_param['width']}x{content_param['height']} • {content_param['bytes'] / 1024:.1f} KB)"
        )
    else:
        print(f"✓ Attached image: {content_param['name']} ({content_param['width']}x{content_param['height']})")

    if ctx.session_id is None:
        s_id = await ctx.mgr.create_session(img_prompt, plan_mode=ctx.active_plan_mode)
        ctx.session_id = s_id
        msgs = ctx.mgr.list_session_messages(s_id)
        if msgs:
            user_msg = next((m for m in reversed(msgs) if m.role == "user"), None)
            if user_msg:
                user_msg.meta = {**(user_msg.meta or {}), "contentParams": [content_param]}
    else:
        user_msg = ctx.mgr._build_message(
            ctx.session_id, "user", img_prompt, meta={"contentParams": [content_param]}
        )
        ctx.mgr._append_message(user_msg)
        await ctx.mgr.reply_session(ctx.session_id, plan_mode=ctx.active_plan_mode)

    if callable(drain_fn):
        await drain_fn(ctx.mgr, ctx.session_id, ctx.yes)
    return SlashAction.HANDLED


# --- Main Dispatcher ---

async def dispatch_slash_command(
    cmd: str,
    cmd_arg: str,
    ctx: ShellContext,
    drain_fn: Any = None,
) -> SlashAction:
    """Dispatches a slash command through the SlashCommandRegistry or fallback handler."""
    cmd_name = cmd.lstrip("/")

    # Check registered command
    registered = registry.find_command(cmd_name)
    if registered is not None:
        func = registered.func
        # Check signature for optional drain_fn
        import inspect

        sig = inspect.signature(func)
        if "drain_fn" in sig.parameters:
            res = func(ctx, cmd_arg, drain_fn=drain_fn)
        else:
            res = func(ctx, cmd_arg)
        if isinstance(res, Coroutine) or inspect.isawaitable(res):
            res = await res
        return res or SlashAction.HANDLED

    # Dynamic /skill:
    if cmd.startswith("/skill:"):
        skill_name = cmd[len("/skill:") :].strip() or cmd_arg.strip()
        if _queue_skill(ctx.mgr, ctx.console, skill_name, ctx.pending_skills):
            if ctx.session_id:
                ctx.mgr.inject_skills(ctx.session_id, ctx.pending_skills)
                ctx.pending_skills.clear()
        return SlashAction.HANDLED

    # Dynamic /flow:
    if cmd.startswith("/flow:"):
        flow_name = cmd[len("/flow:") :].strip() or cmd_arg.strip()
        if not flow_name:
            print("Usage: /flow:<name>")
            return SlashAction.HANDLED
        from coderai.skill.flow.runner import run_flow_skill

        if ctx.session_id is None:
            s_id = await ctx.mgr.create_empty_session(plan_mode=ctx.active_plan_mode)
            ctx.session_id = s_id
        else:
            s_id = ctx.session_id
        outcome = await run_flow_skill(ctx.mgr, s_id, flow_name)
        if outcome.status == "completed":
            print(f"Flow '{flow_name}' completed in {outcome.moves} moves.")
        elif outcome.status == "tool_rejected":
            print(f"Flow '{flow_name}' stopped: tool approval denied.")
        elif outcome.status == "budget_exceeded":
            print(f"Flow '{flow_name}' stopped: {outcome.detail}")
        else:
            print(f"Flow '{flow_name}' failed: {outcome.detail}")
        if callable(drain_fn):
            await drain_fn(ctx.mgr, s_id, ctx.yes)
        return SlashAction.HANDLED

    # Check direct skill alias (e.g. /my-skill)
    if _queue_skill(ctx.mgr, ctx.console, cmd_name, ctx.pending_skills, quiet_unknown=True):
        if ctx.session_id:
            ctx.mgr.inject_skills(ctx.session_id, ctx.pending_skills)
            ctx.pending_skills.clear()
        return SlashAction.HANDLED

    print(f"Unknown command: /{cmd_name}. Type /help for available commands.")
    return SlashAction.HANDLED
