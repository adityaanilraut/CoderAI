"""Interactive selection and inspection menus (/model, /sessions, /skills, /mcp, /config, /tokens, /history)."""

from __future__ import annotations

import os
import sys
from typing import Any

from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from coderai.core.common.model_capabilities import (
    CURATED_MODELS,
    get_model_badges,
)
from coderai.core.session import SessionEntry, SessionManager, SessionMessage
from coderai.core.skill import list_skills

_RICH = True


def _read_single_key() -> str:
    """Read a single keypress or escape sequence from standard input on Unix/Mac or Windows."""
    if not sys.stdin.isatty():
        return ""

    # Windows support
    if os.name == "nt":
        try:
            import msvcrt

            getch_fn = getattr(msvcrt, "getch", None)
            if getch_fn is None:
                return ""
            ch = getch_fn()
            if ch in (b"\x00", b"\xe0"):
                ch2 = getch_fn()
                win_map = {
                    b"H": "UP",
                    b"P": "DOWN",
                    b"K": "LEFT",
                    b"M": "RIGHT",
                    b"G": "HOME",
                    b"O": "END",
                    b"S": "DELETE",
                    b"I": "PAGE_UP",
                    b"Q": "PAGE_DOWN",
                }
                return win_map.get(ch2, "")
            if ch in (b"\r", b"\n"):
                return "ENTER"
            elif ch == b"\x1b":
                return "ESCAPE"
            elif ch == b"\x03":
                return "CTRL_C"
            elif ch == b"\x04":
                return "CTRL_D"
            elif ch in (b"\x08", b"\x7f"):
                return "BACKSPACE"
            elif ch == b"\t":
                return "TAB"
            return ch.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    # Unix / macOS support
    try:
        import select
        import termios
        import tty

        tcgetattr = getattr(termios, "tcgetattr", None)
        tcsetattr = getattr(termios, "tcsetattr", None)
        tcsadrain = getattr(termios, "TCSADRAIN", None)
        setraw = getattr(tty, "setraw", None)

        if not (
            callable(tcgetattr)
            and callable(tcsetattr)
            and callable(setraw)
            and tcsadrain is not None
        ):
            return ""

        fd = sys.stdin.fileno()
        old_settings = tcgetattr(fd)
        try:
            setraw(fd)

            def _read_bytes(n: int) -> bytes:
                is_mock_or_patched = (
                    "mock" in type(sys.stdin).__module__.lower()
                    or type(sys.stdin).__name__ in ("MagicMock", "Mock")
                    or getattr(getattr(sys.stdin.read, "__class__", None), "__name__", "")
                    != "builtin_function_or_method"
                )
                if is_mock_or_patched:
                    try:
                        s = sys.stdin.read(n)
                        if s:
                            return s.encode("utf-8") if isinstance(s, str) else bytes(s)
                    except Exception:
                        pass
                try:
                    raw = os.read(fd, n)
                    if raw:
                        return raw
                except Exception:
                    pass
                try:
                    s = sys.stdin.read(n)
                    return s.encode("utf-8") if isinstance(s, str) else bytes(s)
                except Exception:
                    return b""

            b = _read_bytes(1)
            if not b:
                return ""

            if b == b"\x1b":  # Escape sequence
                try:
                    r, _, _ = select.select([fd], [], [], 0.05)
                except Exception:
                    try:
                        r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    except Exception:
                        r = [1]
                if not r:
                    return "ESCAPE"

                rest = _read_bytes(32)
                if not rest:
                    ch2 = _read_bytes(1)
                    if not ch2:
                        return "ESCAPE"
                    rest = ch2

                # In step-by-step or slow streams, read rest of the sequence
                if rest in (b"[", b"O"):
                    rest += _read_bytes(1)

                if rest.startswith((b"[1", b"[3", b"[4", b"[5", b"[6", b"[7", b"[8")):
                    while not rest.endswith(b"~") and len(rest) < 8:
                        nxt = _read_bytes(1)
                        if not nxt:
                            break
                        rest += nxt

                seq = b + rest
                seq_map: dict[bytes, str] = {
                    b"\x1b[A": "UP",
                    b"\x1bOA": "UP",
                    b"\x1b[1;2A": "UP",
                    b"\x1b[1;5A": "UP",
                    b"\x1b[B": "DOWN",
                    b"\x1bOB": "DOWN",
                    b"\x1b[1;2B": "DOWN",
                    b"\x1b[1;5B": "DOWN",
                    b"\x1b[C": "RIGHT",
                    b"\x1bOC": "RIGHT",
                    b"\x1b[D": "LEFT",
                    b"\x1bOD": "LEFT",
                    b"\x1b[H": "HOME",
                    b"\x1b[1~": "HOME",
                    b"\x1b[7~": "HOME",
                    b"\x1bOH": "HOME",
                    b"\x1b[F": "END",
                    b"\x1b[4~": "END",
                    b"\x1b[8~": "END",
                    b"\x1bOF": "END",
                    b"\x1b[5~": "PAGE_UP",
                    b"\x1b[6~": "PAGE_DOWN",
                    b"\x1b[3~": "DELETE",
                    b"\x1b[Z": "BACKTAB",
                }

                if seq in seq_map:
                    return seq_map[seq]
                for k, v in seq_map.items():
                    if seq.startswith(k):
                        return v
                return "ESCAPE"

            if b in (b"\r", b"\n"):
                return "ENTER"
            elif b == b"\x03":
                return "CTRL_C"
            elif b == b"\x04":
                return "CTRL_D"
            elif b in (b"\x7f", b"\x08"):
                return "BACKSPACE"
            elif b == b"\t":
                return "TAB"

            # Multi-byte UTF-8 handling
            if b and (b[0] & 0x80):
                num_bytes = 1
                if (b[0] & 0xE0) == 0xC0:
                    num_bytes = 2
                elif (b[0] & 0xF0) == 0xE0:
                    num_bytes = 3
                elif (b[0] & 0xF8) == 0xF0:
                    num_bytes = 4
                if num_bytes > 1:
                    b += _read_bytes(num_bytes - 1)

            return b.decode("utf-8", errors="ignore")
        finally:
            if callable(tcsetattr) and tcsadrain is not None:
                tcsetattr(fd, tcsadrain, old_settings)
    except Exception:
        return ""


def select_with_arrows(
    console: Any | None,
    items: list[tuple[str, str, str]],  # (value_key, display_title, description)
    title: str = "Select an option",
    default_idx: int = 0,
    allow_custom: bool = False,
    allow_cancel: bool = False,
) -> int | str | None:
    """Interactive arrow-key and shortcut selector with live in-place TUI rendering and fallback."""
    if not items:
        return None

    if default_idx < 0 or default_idx >= len(items):
        default_idx = 0

    total_slots = len(items) + (1 if allow_custom else 0)

    # If not a TTY, fallback to clean indexed input prompt
    if not sys.stdin.isatty():
        if console is not None and _RICH and Panel is not None:
            body_lines = [
                f"  {num:2}. {disp_title} [dim]— {desc}[/]"
                for num, (_, disp_title, desc) in enumerate(items, 1)
            ]
            if allow_custom:
                body_lines.append(f"  {len(items) + 1:2}. Other / Custom (type custom value)")
            panel = Panel(
                "\n".join(body_lines),
                title=f"[bold magenta]{title}[/]",
                border_style="magenta",
                padding=(0, 1),
            )
            console.print(panel)
        try:
            raw = input(f"{title} [1-{total_slots}]: ").strip()
            if raw.lower() in ("q", "cancel", "exit", "0") and allow_cancel:
                return None
            if raw.isdigit() and 1 <= int(raw) <= len(items):
                return int(raw) - 1
            elif raw.isdigit() and allow_custom and int(raw) == len(items) + 1:
                try:
                    c_val = input("Enter custom value: ").strip()
                    return c_val if c_val else default_idx
                except (EOFError, KeyboardInterrupt):
                    return None if allow_cancel else default_idx
            elif raw:
                return raw
            return None if allow_cancel and not raw else default_idx
        except (EOFError, KeyboardInterrupt):
            return None if allow_cancel else default_idx

    selected_idx = default_idx
    filter_query = ""

    def _render_menu_panel(cur_sel: int, cur_query: str) -> Any:
        if cur_query:
            filtered_indices = [
                i
                for i in range(len(items))
                if cur_query.lower() in items[i][0].lower()
                or cur_query.lower() in items[i][1].lower()
                or cur_query.lower() in items[i][2].lower()
            ]
            if not filtered_indices:
                filtered_indices = [cur_sel] if cur_sel < len(items) else [0]
        else:
            filtered_indices = list(range(len(items)))

        body_lines = []
        if cur_query:
            body_lines.append(f"[dim cyan]Filter:[/] [bold yellow]{cur_query}[/]\n")

        for disp_num, item_idx in enumerate(filtered_indices, 1):
            key_name, disp_title, desc = items[item_idx]
            is_sel = item_idx == cur_sel
            prefix = "[bold cyan]❯[/]" if is_sel else " "
            num_tag = f"[bold cyan]{disp_num:2}.[/]" if is_sel else f"[dim]{disp_num:2}.[/]"
            title_text = f"[bold cyan]{disp_title}[/]" if is_sel else f"[white]{disp_title}[/]"
            desc_text = f" [dim]— {desc}[/]" if desc else ""
            body_lines.append(f"  {prefix} {num_tag} {title_text}{desc_text}")

        if allow_custom:
            custom_num = len(filtered_indices) + 1
            is_sel_custom = cur_sel == len(items)
            prefix = "[bold cyan]❯[/]" if is_sel_custom else " "
            num_tag = (
                f"[bold cyan]{custom_num:2}.[/]" if is_sel_custom else f"[dim]{custom_num:2}.[/]"
            )
            title_style = (
                "[bold cyan]Other / Custom (type custom value)[/]"
                if is_sel_custom
                else "[dim italic]Other / Custom (type custom value)[/]"
            )
            body_lines.append(f"  {prefix} {num_tag} {title_style}")

        if cur_query:
            footer = f"[dim]↑/↓ or 1-9: navigate • Enter: select • Esc: clear filter ({len(filtered_indices)} matches) • Backspace: delete[/]"
        else:
            footer = "[dim]↑/↓ or 1-9: navigate • Type to search • Enter: select • Esc/q: cancel[/]"

        if Panel is not None:
            return Panel(
                "\n".join(body_lines) + f"\n\n{footer}",
                title=f"[bold magenta]{title}[/]",
                border_style="magenta",
                padding=(0, 1),
            )
        return "\n".join(body_lines) + f"\n\n{footer}"

    def _get_filtered(cur_query: str) -> list[int]:
        if cur_query:
            f_inds = [
                i
                for i in range(len(items))
                if cur_query.lower() in items[i][0].lower()
                or cur_query.lower() in items[i][1].lower()
                or cur_query.lower() in items[i][2].lower()
            ]
            return f_inds if f_inds else ([selected_idx] if selected_idx < len(items) else [0])
        return list(range(len(items)))

    def _get_selectable(cur_query: str) -> list[int]:
        f_inds = _get_filtered(cur_query)
        if allow_custom:
            return f_inds + [len(items)]
        return f_inds

    # Use Live rendering if available for in-place updates without scroll spam
    if Live is not None and console is not None and _RICH:
        with Live(
            _render_menu_panel(selected_idx, filter_query),
            console=console,
            transient=True,
            auto_refresh=False,
        ) as live:
            res: int | None = None
            while True:
                selectable = _get_selectable(filter_query)
                filtered_indices = _get_filtered(filter_query)
                if selected_idx not in selectable and selectable:
                    selected_idx = selectable[0]

                live.update(_render_menu_panel(selected_idx, filter_query), refresh=True)

                key = _read_single_key()
                if key == "UP":
                    if selectable:
                        if selected_idx in selectable:
                            cur_pos = selectable.index(selected_idx)
                            selected_idx = selectable[(cur_pos - 1) % len(selectable)]
                        else:
                            selected_idx = selectable[-1]
                elif key == "DOWN":
                    if selectable:
                        if selected_idx in selectable:
                            cur_pos = selectable.index(selected_idx)
                            selected_idx = selectable[(cur_pos + 1) % len(selectable)]
                        else:
                            selected_idx = selectable[0]
                elif key == "PAGE_UP":
                    if selectable:
                        if selected_idx in selectable:
                            cur_pos = selectable.index(selected_idx)
                            selected_idx = selectable[max(0, cur_pos - 5)]
                        else:
                            selected_idx = selectable[0]
                elif key == "PAGE_DOWN":
                    if selectable:
                        if selected_idx in selectable:
                            cur_pos = selectable.index(selected_idx)
                            selected_idx = selectable[min(len(selectable) - 1, cur_pos + 5)]
                        else:
                            selected_idx = selectable[-1]
                elif key == "HOME":
                    if selectable:
                        selected_idx = selectable[0]
                elif key == "END":
                    if selectable:
                        selected_idx = selectable[-1]
                elif key.isdigit() and not filter_query:
                    digit = int(key)
                    if 1 <= digit <= len(filtered_indices):
                        selected_idx = filtered_indices[digit - 1]
                    elif allow_custom and digit == len(filtered_indices) + 1:
                        selected_idx = len(items)
                elif key == "ENTER":
                    if allow_custom and selected_idx == len(items):
                        live.stop()
                        try:
                            custom_val = input("\nEnter custom value: ").strip()
                            return custom_val if custom_val else default_idx
                        except (EOFError, KeyboardInterrupt):
                            return None if allow_cancel else default_idx
                    res = selected_idx
                    break
                elif key == "ESCAPE":
                    if filter_query:
                        filter_query = ""
                    else:
                        res = None if allow_cancel else default_idx
                        break
                elif key in ("CTRL_C", "CTRL_D") or (key in ("q", "Q") and not filter_query):
                    res = None if allow_cancel else default_idx
                    break
                elif key == "BACKSPACE":
                    filter_query = filter_query[:-1]
                elif len(key) == 1 and key.isprintable():
                    filter_query += key

        # Confirmation event line
        if isinstance(res, int) and 0 <= res < len(items):
            chosen_label = items[res][1]
            console.print(f"  [bold green]●[/] [dim]Selected:[/] [bold cyan]{chosen_label}[/]")
        return res

    while True:
        selectable = _get_selectable(filter_query)
        filtered_indices = _get_filtered(filter_query)
        if selected_idx not in selectable and selectable:
            selected_idx = selectable[0]

        print(f"\n--- {title} ---")
        for disp_num, item_idx in enumerate(filtered_indices, 1):
            key_name, disp_title, desc = items[item_idx]
            marker = "❯" if item_idx == selected_idx else " "
            print(f" {marker} {disp_num:2}. {disp_title} — {desc}")
        if allow_custom:
            custom_num = len(filtered_indices) + 1
            marker = "❯" if selected_idx == len(items) else " "
            print(f" {marker} {custom_num:2}. Other / Custom (type custom value)")
        if filter_query:
            print(
                f"  Filter: '{filter_query}' ({len(filtered_indices)} matches) | Esc: clear filter"
            )
        else:
            print("  ↑/↓ or 1-9: navigate, Type to search, Enter: select, Esc/q: cancel")

        key = _read_single_key()
        if key == "UP":
            if selectable:
                if selected_idx in selectable:
                    cur_pos = selectable.index(selected_idx)
                    selected_idx = selectable[(cur_pos - 1) % len(selectable)]
                else:
                    selected_idx = selectable[-1]
        elif key == "DOWN":
            if selectable:
                if selected_idx in selectable:
                    cur_pos = selectable.index(selected_idx)
                    selected_idx = selectable[(cur_pos + 1) % len(selectable)]
                else:
                    selected_idx = selectable[0]
        elif key == "PAGE_UP":
            if selectable:
                if selected_idx in selectable:
                    cur_pos = selectable.index(selected_idx)
                    selected_idx = selectable[max(0, cur_pos - 5)]
                else:
                    selected_idx = selectable[0]
        elif key == "PAGE_DOWN":
            if selectable:
                if selected_idx in selectable:
                    cur_pos = selectable.index(selected_idx)
                    selected_idx = selectable[min(len(selectable) - 1, cur_pos + 5)]
                else:
                    selected_idx = selectable[-1]
        elif key == "HOME":
            if selectable:
                selected_idx = selectable[0]
        elif key == "END":
            if selectable:
                selected_idx = selectable[-1]
        elif key.isdigit() and not filter_query:
            digit = int(key)
            if 1 <= digit <= len(filtered_indices):
                selected_idx = filtered_indices[digit - 1]
            elif allow_custom and digit == len(filtered_indices) + 1:
                selected_idx = len(items)
        elif key == "ENTER":
            if allow_custom and selected_idx == len(items):
                try:
                    custom_val = input("\nEnter custom value: ").strip()
                    return custom_val if custom_val else default_idx
                except (EOFError, KeyboardInterrupt):
                    return None if allow_cancel else default_idx
            return selected_idx
        elif key == "ESCAPE":
            if filter_query:
                filter_query = ""
            else:
                return None if allow_cancel else default_idx
        elif key in ("CTRL_C", "CTRL_D") or (key in ("q", "Q") and not filter_query):
            return None if allow_cancel else default_idx
        elif key == "BACKSPACE":
            filter_query = filter_query[:-1]
        elif len(key) == 1 and key.isprintable():
            filter_query += key


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
    """Prompt the user with an interactive model selection menu with arrow-key navigation."""
    items: list[tuple[str, str, str]] = []
    default_idx = 0
    for idx, (name, desc, category) in enumerate(CURATED_MODELS):
        badges = get_model_badges(name)
        badges_str = " ".join(f"[{b}]" for b in badges)
        items.append((name, f"{name:<20} {badges_str}", f"[{category}] {desc}"))
        if name == current_model:
            default_idx = idx

    res = select_with_arrows(
        console,
        items,
        title=f"Select Active Model (Current: {current_model})",
        default_idx=default_idx,
        allow_custom=True,
        allow_cancel=True,
    )
    if res is None:
        return current_model
    if isinstance(res, int) and 0 <= res < len(CURATED_MODELS):
        return CURATED_MODELS[res][0]
    elif isinstance(res, str) and res.strip():
        val = res.strip()
        from coderai.cli.fuzzy import fuzzy_filter

        model_names = [name for name, _, _ in CURATED_MODELS]
        fuzzy_models = fuzzy_filter(val, model_names, limit=1)
        if fuzzy_models:
            return fuzzy_models[0]
        return val
    return current_model


REASONING_EFFORT_CHOICES: list[tuple[str, str, str, str]] = [
    (
        "max",
        "Max Reasoning Depth",
        "Uncapped chain-of-thought tokens (up to 64k), best for complex SWE-bench & architecture",
        "[bold magenta]Max[/]",
    ),
    (
        "high",
        "High Reasoning Depth",
        "Deep multi-step reasoning (~16k-24k tokens), ideal for algorithms & security reviews",
        "[magenta]High[/]",
    ),
    (
        "medium",
        "Medium Reasoning Depth",
        "Balanced reasoning (~4k-8k tokens), standard for everyday coding & refactoring",
        "[cyan]Medium[/]",
    ),
    (
        "low",
        "Low Reasoning Depth",
        "Fast light reasoning (~1k-2k tokens), ultra-low latency for syntax & quick fixes",
        "[yellow]Low[/]",
    ),
    (
        "off",
        "Disable Reasoning (Off)",
        "Direct model response without hidden chain-of-thought tokens (0 reasoning tokens)",
        "[dim]Off[/]",
    ),
]


def select_reasoning_effort_interactive(
    console: Any | None, current_effort: str = "max", model: str = ""
) -> str:
    """Prompt the user with an interactive reasoning effort selection menu."""
    norm_cur = (current_effort or "max").strip().lower()
    title_extra = f" for '{model}'" if model else ""

    items = [(tag, f"{name} ({tag})", desc) for tag, name, desc, _ in REASONING_EFFORT_CHOICES]
    default_idx = next((i for i, (t, _, _) in enumerate(items) if t == norm_cur), 0)

    res = select_with_arrows(
        console,
        items,
        title=f"Select Reasoning Effort{title_extra}",
        default_idx=default_idx,
    )
    if isinstance(res, int) and 0 <= res < len(items):
        return items[res][0]
    elif isinstance(res, str) and res:
        from coderai.core.common.openai_thinking import normalize_reasoning_effort

        return normalize_reasoning_effort(res)
    return norm_cur


def select_session_interactive(console: Any | None, sessions: list[SessionEntry]) -> str | None:
    """Display interactive sessions list and allow selecting a session to resume, delete, or fork."""
    if not sessions:
        if console is not None and _RICH:
            console.print("[dim]No saved sessions found in workspace.[/]")
        else:
            print("No saved sessions found in workspace.")
        return None

    from coderai.cli.fuzzy import fuzzy_filter

    # If interactive TUI is active, use arrow-key selector with live search
    if sys.stdin.isatty() and console is not None and _RICH:
        items: list[tuple[str, str, str]] = []
        for s in sessions:
            plan_str = " [bold yellow]● Plan[/]" if s.plan_mode else ""
            status_str = f"[dim]({s.status})[/]" if s.status != "idle" else ""
            disp_title = f"{s.id[:14]}  [bold green]{s.active_tokens:,} tokens[/]{plan_str} {status_str}".strip()
            desc = s.summary or "(no summary)"
            items.append((s.id, disp_title, desc))

        res = select_with_arrows(
            console,
            items,
            title=f"Saved Sessions ({len(sessions)})",
            default_idx=0,
            allow_cancel=True,
        )
        if res is None:
            return None

        if isinstance(res, int) and 0 <= res < len(sessions):
            chosen_session = sessions[res]
            # Prompt for action on chosen session
            action_options = [
                (
                    "resume",
                    "Resume Session",
                    f"Continue working in session {chosen_session.id[:14]}",
                ),
                (
                    "fork",
                    "Fork Session",
                    "Branch into a new session copying history and checkpoint",
                ),
                ("delete", "Delete Session", "Remove session from workspace"),
            ]
            action_res = select_with_arrows(
                console,
                action_options,
                title=f"Session Action for {chosen_session.id[:14]}",
                default_idx=0,
                allow_cancel=True,
            )
            if action_res == 0 or action_res == "resume":
                return chosen_session.id
            elif action_res == 1 or action_res == "fork":
                return f"fork:{chosen_session.id}"
            elif action_res == 2 or action_res == "delete":
                return f"delete:{chosen_session.id}"
            return chosen_session.id if action_res is not None else None
        elif isinstance(res, str) and res:
            matched = next((s.id for s in sessions if s.id.startswith(res)), res)
            return matched

    current_page = 0
    page_size = 10
    active_filter = ""

    while True:
        # Apply filter if set
        if active_filter:
            display_sessions = fuzzy_filter(
                active_filter,
                sessions,
                key_func=lambda s: f"{s.id} {s.summary or ''} {s.status}",
                limit=len(sessions),
            )
        else:
            display_sessions = list(sessions)

        total_sessions = len(display_sessions)
        total_pages = max(1, (total_sessions + page_size - 1) // page_size)
        if current_page >= total_pages:
            current_page = total_pages - 1
        if current_page < 0:
            current_page = 0

        start_idx = current_page * page_size
        end_idx = min(start_idx + page_size, total_sessions)
        page_items = display_sessions[start_idx:end_idx]

        filter_tag = f" [cyan](Filter: '{active_filter}')[/]" if active_filter else ""
        title = f"Saved Sessions — Page {current_page + 1}/{total_pages} (Total: {total_sessions}){filter_tag}"

        if console is not None and _RICH and Table is not None:
            table = Table(title=title, border_style="blue")
            table.add_column("#", style="bold cyan", width=4)
            table.add_column("Session ID", style="bold white", width=16)
            table.add_column("Status", style="yellow", width=12)
            table.add_column("Plan", style="magenta", width=6)
            table.add_column("Tokens", style="green", width=10)
            table.add_column("Summary", style="white")

            for page_rel_idx, s in enumerate(page_items, 1):
                abs_idx = start_idx + page_rel_idx
                table.add_row(
                    str(abs_idx),
                    s.id[:14],
                    s.status,
                    "✓" if s.plan_mode else "—",
                    f"{s.active_tokens:,}",
                    (s.summary or "(no summary)")[:50],
                )
            console.print(table)
            nav_hints = []
            if current_page < total_pages - 1:
                nav_hints.append("[bold cyan]n[/] next page")
            if current_page > 0:
                nav_hints.append("[bold cyan]p[/] prev page")
            nav_str = (" • " + " • ".join(nav_hints)) if nav_hints else ""
            console.print(
                f"[dim]Actions: [bold cyan]<num>[/] resume • [bold red]d <num>[/] delete • [bold yellow]f <num>[/] fork • [bold blue]s <query>[/] search{nav_str} • [bold]Enter[/] cancel[/]"
            )
        else:
            print(f"\n--- {title} ---")
            for page_rel_idx, s in enumerate(page_items, 1):
                abs_idx = start_idx + page_rel_idx
                plan_str = "[plan]" if s.plan_mode else "      "
                print(
                    f"  {abs_idx:2}. {s.id[:14]}  {s.status:10} {plan_str}  {s.active_tokens:6} tokens  {(s.summary or '')[:40]}"
                )
            print(
                "Actions: <num> resume | d <num> delete | f <num> fork | s <query> search | n/p page | Enter cancel"
            )

        try:
            raw_choice = input(
                f"\nSelect session action [1-{total_sessions}, d <num>, f <num>, s <query>, n/p]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not raw_choice:
            return None

        # Pagination commands
        if raw_choice.lower() in ("n", "next") and current_page < total_pages - 1:
            current_page += 1
            continue
        if raw_choice.lower() in ("p", "prev", "previous") and current_page > 0:
            current_page -= 1
            continue

        # Search filter command: "s <query>" or "/search <query>"
        if raw_choice.startswith(("s ", "search ", "/search ", "find ")):
            active_filter = (
                raw_choice.split(None, 1)[1].strip() if len(raw_choice.split()) > 1 else ""
            )
            current_page = 0
            continue
        if raw_choice.lower() in ("s", "clear", "all", "reset"):
            active_filter = ""
            current_page = 0
            continue

        # Handle delete action: "d 1" or "del 1"
        if (
            raw_choice.startswith(("d ", "del ", "delete ", "d", "del", "delete"))
            and len(raw_choice.split()) > 1
        ):
            parts = raw_choice.split()
            if parts[1].isdigit():
                idx = int(parts[1])
                if 1 <= idx <= len(display_sessions):
                    return f"delete:{display_sessions[idx - 1].id}"

        # Handle fork action: "f 1" or "fork 1"
        if raw_choice.startswith(("f ", "fork ", "f", "fork")) and len(raw_choice.split()) > 1:
            parts = raw_choice.split()
            if parts[1].isdigit():
                idx = int(parts[1])
                if 1 <= idx <= len(display_sessions):
                    return f"fork:{display_sessions[idx - 1].id}"

        # Standard resume by number
        if raw_choice.isdigit():
            idx = int(raw_choice)
            if 1 <= idx <= len(display_sessions):
                return display_sessions[idx - 1].id

        # If user typed raw session id or fuzzy query
        matched_sessions = fuzzy_filter(
            raw_choice,
            sessions,
            key_func=lambda s: f"{s.id} {s.summary or ''}",
            limit=1,
        )
        if matched_sessions:
            return matched_sessions[0].id

        matched_prefix = next((s.id for s in sessions if s.id.startswith(raw_choice)), None)
        return matched_prefix


def prompt_plan_implementation(console: Any | None, plan_text: str = "") -> str:
    """Prompt the user for next action when a plan has been proposed."""
    options = [
        (
            "execute",
            "Yes, execute plan now",
            "turn off Plan Mode and begin implementation automatically",
        ),
        ("refine", "Refine plan", "specify adjustments and stay in Plan Mode"),
        ("stay", "No, stay in plan mode", "keep exploring before executing"),
    ]
    res = select_with_arrows(
        console,
        options,
        title="Plan Ready — Next Action",
        default_idx=0,
    )
    if isinstance(res, int) and 0 <= res < len(options):
        return options[res][0]
    elif isinstance(res, str) and res in ("execute", "refine", "stay"):
        return res
    elif isinstance(res, str) and res.lower() in ("1", "y", "yes", "execute"):
        return "execute"
    elif isinstance(res, str) and res.lower() in ("2", "r", "refine"):
        return "refine"
    elif isinstance(res, str) and res.lower() in ("3", "s", "n", "no", "stay"):
        return "stay"
    return "stay"


def select_undo_interactive(
    console: Any | None, targets: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """Display interactive turn selector and restore mode picker for /undo."""
    if not targets:
        return None, "restore_both"

    if len(targets) == 1:
        chosen_target = targets[0]
    else:
        items = []
        for t in targets:
            idx = t.get("index", 1)
            prompt_snip = t.get("prompt", "")[:50]
            ckpt = (t.get("checkpoint_hash") or "—")[:10]
            has_code = "✓ code backup" if t.get("can_restore_code") else "conv only"
            items.append((str(idx), f"Turn #{idx}: {prompt_snip}", f"ckpt: {ckpt} [{has_code}]"))

        res = select_with_arrows(
            console,
            items,
            title="Select Turn Checkpoint to Restore",
            default_idx=len(items) - 1,
        )
        if isinstance(res, int) and 0 <= res < len(targets):
            chosen_target = targets[res]
        elif isinstance(res, str) and res.isdigit() and 1 <= int(res) <= len(targets):
            chosen_target = targets[int(res) - 1]
        else:
            return None, "restore_both"

    # Mode picker
    mode_options = [
        (
            "restore_both",
            "Restore Conversation + Code",
            "Roll back conversation and reset workspace files to checkpoint",
        ),
        (
            "restore_conversation_only",
            "Restore Conversation Only",
            "Truncate conversation history but keep current workspace files",
        ),
        (
            "restore_code_only",
            "Restore Code Only",
            "Reset files to checkpoint but keep full conversation history",
        ),
    ]
    mode_res = select_with_arrows(
        console,
        mode_options,
        title="Select Restore Mode",
        default_idx=0,
    )
    if isinstance(mode_res, int) and 0 <= mode_res < len(mode_options):
        return chosen_target, mode_options[mode_res][0]
    elif isinstance(mode_res, str) and mode_res in (
        "restore_both",
        "restore_conversation_only",
        "restore_code_only",
    ):
        return chosen_target, mode_res
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
                sk.get("name", ""),
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
            print(f"  {sk.get('name', '')}: {sk.get('description', '')} [{sk.get('location', '')}]")
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
        mask_api_key,
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
        table.add_row("Base URL:", str(settings.get("baseURL", "default")))
        api_k = settings.get("apiKey")
        table.add_row(
            "API Key:", f"[bold green]{mask_api_key(api_k)}[/]" if api_k else "[dim red]Not set[/]"
        )
        perms = settings.get("permissions") or {}
        table.add_row("Permission Mode:", str(perms.get("defaultMode", "allowAll")))
        table.add_row("Reasoning Effort:", str(settings.get("reasoningEffort", "max")))
        table.add_row("Context Window:", f"{settings.get('contextWindow', 262144):,} tokens")
        table.add_row(
            "Auto-Compact Window:", f"{settings.get('autoCompactWindow', 131072):,} tokens"
        )

        allows = perms.get("allow") or []
        allows_str = ", ".join(allows) if allows else "none"
        table.add_row("Allowed Scopes:", f"[bold green]{allows_str}[/]")

        mcp_servers = (
            list(settings.get("mcpServers", {}).keys()) if settings.get("mcpServers") else []
        )
        table.add_row("Configured MCP Servers:", ", ".join(mcp_servers) if mcp_servers else "none")
        table.add_row("User Config File:", f"[dim]{user_path}[/]")
        table.add_row("Project Config File:", f"[dim]{proj_path}[/]")

        panel = Panel(
            table,
            title="[bold cyan]CoderAI Active Configuration[/] [dim]• Tip: Run [bold yellow]/setup[/bold yellow] to change keys/models[/dim]",
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
        print(f"  Project Config: {proj_path}")
        print("  Tip: Run /setup to change keys and models.\n")


MODEL_PRICING_PER_M: dict[str, tuple[float, float, float]] = {
    # $/million tokens: (prompt, completion, cached)
    "gpt-5.6-sol": (2.50, 10.00, 1.25),
    "gpt-5.6-terra": (1.75, 7.00, 0.875),
    "gpt-5.6-luna": (1.25, 5.00, 0.625),
    "gemini-3.7-flash": (0.10, 0.40, 0.025),
    "deepseek-v4-pro": (0.55, 2.19, 0.14),
    "deepseek-v4-flash": (0.14, 0.28, 0.014),
    "default": (2.00, 8.00, 1.00),
}


def estimate_model_cost(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """Calculate estimated cost in USD based on model pricing table."""
    rates = MODEL_PRICING_PER_M.get(model_name.lower())
    if not rates:
        # Check prefix match
        matched_key = next((k for k in MODEL_PRICING_PER_M if k in model_name.lower()), "default")
        rates = MODEL_PRICING_PER_M[matched_key]

    p_rate, c_rate, cached_rate = rates
    effective_prompt = max(0, prompt_tokens - cached_tokens)
    cost = (
        (effective_prompt * p_rate / 1_000_000)
        + (cached_tokens * cached_rate / 1_000_000)
        + (completion_tokens * c_rate / 1_000_000)
    )
    return cost


def render_token_breakdown(
    console: Any | None, mgr: SessionManager, session_id: str | None
) -> None:
    """Display session token analytics, estimated cost in USD, and context window usage."""
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

    active_model = mgr.get_active_model()
    usage = entry.usage or {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    active_tokens = entry.active_tokens

    # Estimate session cost
    total_cost = estimate_model_cost(active_model, prompt_tokens, completion_tokens, cached_tokens)

    # Default context window ~256k
    from coderai.core.settings import get_default_context_window

    max_context = get_default_context_window(active_model)
    pct_used = (active_tokens / max_context) * 100 if max_context > 0 else 0.0

    if console is not None and _RICH and Panel is not None and Table is not None:
        table = Table.grid(padding=(0, 2))
        table.add_column("Metric", style="dim cyan", width=22)
        table.add_column("Tokens / Value", style="bold white")

        table.add_row("Active Model:", f"[bold cyan]{active_model}[/]")
        table.add_row("Prompt Tokens:", f"{prompt_tokens:,}")
        table.add_row("Completion Tokens:", f"{completion_tokens:,}")
        if cached_tokens > 0:
            hit_rate = (cached_tokens / prompt_tokens * 100.0) if prompt_tokens > 0 else 0.0
            table.add_row("Cached Tokens:", f"[green]{cached_tokens:,}[/]")
            table.add_row("Cache Hit Rate:", f"[bold green]{hit_rate:.1f}%[/]")
        table.add_row("Total Session Tokens:", f"[bold cyan]{total_tokens:,}[/]")
        table.add_row(
            "Active Working Context:",
            f"[bold green]{active_tokens:,}[/] / {max_context:,} ({pct_used:.1f}%)",
        )
        table.add_row("Estimated Session Cost:", f"[bold green]${total_cost:.4f} USD[/]")

        if entry.usage_per_model:
            table.add_row("Usage by Model:", "")
            for model_name, m_usage in entry.usage_per_model.items():
                m_total = m_usage.get("total_tokens", 0) if isinstance(m_usage, dict) else 0
                m_prompt = m_usage.get("prompt_tokens", 0) if isinstance(m_usage, dict) else 0
                m_comp = m_usage.get("completion_tokens", 0) if isinstance(m_usage, dict) else 0
                m_cached = m_usage.get("cached_tokens", 0) if isinstance(m_usage, dict) else 0
                m_cost = estimate_model_cost(model_name, m_prompt, m_comp, m_cached)
                table.add_row(f"  • {model_name}:", f"{m_total:,} tokens (${m_cost:.4f})")

        panel = Panel(
            table,
            title=f"[bold green]Token Usage & Cost Analytics[/] [dim]({session_id[:12]})[/]",
            border_style="green",
            padding=(0, 1),
        )
        console.print()
        console.print(panel)
    else:
        print(f"\n--- Token Usage & Cost: {session_id[:12]} ---")
        print(f"  Active Model:      {active_model}")
        print(f"  Prompt Tokens:     {prompt_tokens:,}")
        print(f"  Completion Tokens: {completion_tokens:,}")
        if cached_tokens > 0:
            hit_rate = (cached_tokens / prompt_tokens * 100.0) if prompt_tokens > 0 else 0.0
            print(f"  Cached Tokens:     {cached_tokens:,}")
            print(f"  Cache Hit Rate:    {hit_rate:.1f}%")
        print(f"  Total Tokens:      {total_tokens:,}")
        print(f"  Active Context:    {active_tokens:,} ({pct_used:.1f}%)")
        print(f"  Estimated Cost:    ${total_cost:.4f} USD\n")


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
