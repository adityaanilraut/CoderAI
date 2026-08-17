"""Interactive selection menus for slash commands (/model, /sessions, /skills)."""

from __future__ import annotations

from typing import Any

from coderai.core.common.model_capabilities import (
    format_capability_badges,
    get_model_badges,
)
from coderai.core.prompt import list_skills
from coderai.core.session import SessionEntry

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

CURATED_MODELS: list[tuple[str, str, str]] = [
    # OpenAI GPT-5.6 Tiered Series & Reasoning
    ("gpt-5.6-sol", "Flagship Tier: Deep reasoning, complex agentic coding", "OpenAI GPT-5.6"),
    ("gpt-5.6-terra", "Balanced Tier: Everyday coding, cost/speed balanced", "OpenAI GPT-5.6"),
    ("gpt-5.6-luna", "Fast Tier: Ultra-low latency, inline edits & suggestions", "OpenAI GPT-5.6"),
    ("o3-mini", "Deep multi-step reasoning with thinking traces", "OpenAI Reasoning"),
    ("o1", "Deep reasoning for complex algorithms", "OpenAI Reasoning"),
    ("gpt-4o", "Standard multimodal legacy tier (Default)", "OpenAI Legacy"),
    ("gpt-4o-mini", "Fast, lightweight multimodal legacy tier", "OpenAI Legacy"),
    # Anthropic Claude (Hybrid Reasoning)
    ("claude-3-7-sonnet", "Flagship hybrid reasoning with extended thinking", "Anthropic Claude"),
    ("claude-3-5-sonnet", "Industry-standard coding benchmark leader", "Anthropic Claude"),
    ("claude-3-5-haiku", "High-speed, lightweight sub-agent worker", "Anthropic Claude"),
    # Google Gemini (2.5 Lineup)
    ("gemini-2.5-pro", "2M+ context window, deep logic & repo ingestion", "Google Gemini 2.5"),
    ("gemini-2.5-flash", "Sub-second response with built-in visible thinking", "Google Gemini 2.5"),
    (
        "gemini-2.5-flash-lite",
        "Ultra-efficient lightweight model for high-frequency tooling",
        "Google Gemini 2.5",
    ),
    # DeepSeek (V4 & R1 Reasoning)
    (
        "deepseek-v4-pro",
        "Flagship agentic coding & deep reasoning (1M context)",
        "DeepSeek V4 & R1",
    ),
    ("deepseek-v4-flash", "High-throughput coding & tool-calling engine", "DeepSeek V4 & R1"),
    ("deepseek-r1", "Open reasoning with detailed chain-of-thought", "DeepSeek V4 & R1"),
]


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

    # Direct model name typed by user
    return choice


def select_session_interactive(console: Any | None, sessions: list[SessionEntry]) -> str | None:
    """Display interactive sessions list and allow selecting a session to resume."""
    if not sessions:
        if console is not None and _RICH:
            console.print("[dim]No saved sessions found in workspace.[/]")
        else:
            print("No saved sessions found in workspace.")
        return None

    if console is not None and _RICH and Table is not None:
        table = Table(title="Interactive Sessions Menu", border_style="blue")
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Session ID", style="bold white", width=14)
        table.add_column("Status", style="yellow", width=12)
        table.add_column("Plan", style="magenta", width=6)
        table.add_column("Tokens", style="green", width=10)
        table.add_column("Summary", style="white")

        for idx, s in enumerate(sessions, 1):
            table.add_row(
                str(idx),
                s.id[:12],
                s.status,
                "✓" if s.plan_mode else "—",
                f"{s.active_tokens:,}",
                (s.summary or "(no summary)")[:50],
            )
        console.print(table)
    else:
        print("\n--- Saved Sessions ---")
        for idx, s in enumerate(sessions, 1):
            plan_str = "[plan]" if s.plan_mode else "      "
            print(
                f"  {idx:2}. {s.id[:12]}  {s.status:10} {plan_str}  {s.active_tokens:6} tokens  {(s.summary or '')[:40]}"
            )

    try:
        raw_choice = input(
            f"\nSelect session number to resume [1-{len(sessions)}, Enter to cancel]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw_choice:
        return None

    if raw_choice.isdigit():
        idx = int(raw_choice)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1].id

    # If user passed raw session id directly
    matched = next((s.id for s in sessions if s.id.startswith(raw_choice)), None)
    return matched


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
    else:
        print(f"\n--- Discovered Skills ({len(skills)}) ---")
        for sk in skills:
            print(
                f"  ⚡ {sk.get('name', '')}: {sk.get('description', '')} [{sk.get('location', '')}]"
            )
        print()
