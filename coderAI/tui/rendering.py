"""Rendering helpers extracted from CoderAIApp.

Each function takes the minimal state it needs and returns a markup
string.  CoderAIApp methods delegate to these.
"""

from __future__ import annotations

import re
from typing import Any

from rich.console import RenderableType
from rich.markup import escape
from rich.tree import Tree

from coderAI.tui.platform import composer_footer_hints
from coderAI.tui.state import SessionState
from coderAI.tui.theme import Glyphs, Styles, Tokens

_RICH_TAG_RE = re.compile(r"\[/?[a-zA-Z][a-zA-Z0-9 _#\-/]*\]")


def strip_rich_markup(text: Any) -> str:
    """Strip Rich ``[style]`` tags from ``text`` for plain-text output."""
    if text is None:
        return ""
    s = str(text)
    if "[" not in s:
        return s
    return _RICH_TAG_RE.sub("", s)


def render_session_header(s: SessionState) -> str:
    """Return markup for the top session header bar."""
    status_color = Tokens.AGENT if s.streaming or s.thinking else Tokens.TEXT_DIM
    ctx_used = f"{s.ctx_used:,}" if s.ctx_used else "0"
    ctx_lim = f"{s.ctx_limit // 1000}k" if s.ctx_limit else "?"
    model_label = s.model or "…"

    def chip(label: str, value: str, color: str = Tokens.TEXT, bar: float = -1) -> str:
        inner = f"[{Tokens.TEXT_MUTED}]{label}[/] [{color}]{value}[/]"
        if bar >= 0:
            w = 10
            f = min(w, max(0, int(bar * w)))
            b = f"[{color}]" + ("█" * f) + "[/]"
            b += f"[{Tokens.LINE}]" + ("░" * (w - f)) + "[/]"
            inner += f" {b}"
        return inner

    ctx_ratio = (s.ctx_used / max(1, s.ctx_limit)) if s.ctx_limit else 0
    # Color is earned: normal until 80%, amber to 90%, then danger.
    if ctx_ratio >= 0.9:
        ctx_color = Tokens.DANGER
    elif ctx_ratio >= 0.8:
        ctx_color = Tokens.WARN
    else:
        ctx_color = Tokens.TEXT
    budget_ratio = (s.cost_usd / s.budget_usd) if s.budget_usd and s.budget_usd > 0 else -1
    cost_val = (
        f"${s.cost_usd:.4f} / ${s.budget_usd:.2f}"
        if s.budget_usd and s.budget_usd > 0
        else f"${s.cost_usd:.4f}"
    )

    chips = [
        f"[{status_color}]{Glyphs.BRAND}[/] [{Tokens.TEXT}]{model_label}[/]",
        chip("ctx", f"{ctx_used} / {ctx_lim}", ctx_color, bar=ctx_ratio),
        chip("$", cost_val, Tokens.TEXT_DIM, bar=budget_ratio),
    ]
    working = s.streaming or s.thinking
    if working and s.iteration > 0:
        chips.append(chip("iter", f"{s.iteration}/{s.max_iterations}", Tokens.TEXT_DIM))
    if working and s.elapsed_s > 0:
        m, sec = divmod(int(s.elapsed_s), 60)
        ts = f"{m}m {sec}s" if m > 0 else f"{sec}s"
        chips.append(chip("t", ts, Tokens.TEXT_MUTED))
    active = sum(1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled"))
    if active > 1:
        chips.append(chip("agents", f"{active} active", Tokens.AGENT))
    if s.workspace_trusted is False:
        chips.append(
            chip(
                "workspace",
                "untrusted",
                Tokens.DANGER,
            )
        )
    if s.auto_approve:
        chips.append(chip("yolo", "on", Tokens.WARN))
    if s.reasoning and s.reasoning != "none":
        chips.append(chip("reason", s.reasoning, Tokens.THOUGHT))
    if s.active_persona:
        chips.append(chip("persona", s.active_persona, Tokens.INFO))
    if s.progress:
        prog = s.progress
        label = escape(str(prog.get("label") or "Working"))
        current = prog.get("current")
        total = prog.get("total")
        if current is not None and total is not None:
            chips.append(chip("progress", f"{label} {current}/{total}", Tokens.AGENT))
        else:
            chips.append(chip("progress", label, Tokens.AGENT))

    left = f" [{Tokens.TEXT_MUTED}]•[/] ".join(chips)
    return left


def render_agent_tree(s: SessionState) -> RenderableType:
    """Return a compact Rich Tree for the left agent panel.

    Now shows per-agent tokens/cost and expands to reveal the full prompt when
    available. Header chips wrap via the Tree title; virtualization is
    delegated to the VerticalScroll container (no full re-render for 100+).
    """
    active_count = sum(
        1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled")
    )
    # Header chips with wrap: use soft line breaks rather than one long chip row
    title = f"[{Styles.SECTION}]AGENTS[/]  [{Tokens.TEXT_MUTED}]· {active_count} active[/]"
    if s.agents and any(a.total_tokens or a.cost_usd for a in s.agents.values()):
        total_tok = sum(a.total_tokens for a in s.agents.values())
        total_cost = sum(a.cost_usd for a in s.agents.values())
        title += f"  [{Tokens.TEXT_MUTED}]{total_tok:,} tok · ${total_cost:.2f}[/]"
    if not s.agents:
        return title + f"\n[{Tokens.TEXT_MUTED}](no agents yet)[/]"

    # Virtualized: only render first 100 + overflow indicator to keep scroll cheap
    VIRTUAL_LIMIT = 100
    tree = Tree(title, guide_style=Tokens.LINE)
    agents = list(s.agents.values())
    seen: set[str] = set()

    def add_node(parent_node: Tree, aid: str, depth: int = 0) -> None:
        if aid in seen:
            return
        info = s.agents.get(aid)
        if info is None:
            return
        seen.add(aid)

        status = info.status
        if status in ("thinking", "tool_call"):
            color = Tokens.AGENT if status == "tool_call" else Tokens.THOUGHT
            glow = True
        elif status == "waiting_for_user":
            color = Tokens.WARN
            glow = True
        elif status in ("done", "cancelled"):
            color = Tokens.TEXT_MUTED
            glow = False
        elif status == "error":
            color = Tokens.DANGER
            glow = False
        else:
            color = Tokens.WARN
            glow = False

        name = info.name or info.id
        task = (info.task or "")[:28]
        dot = f"[{color}]" + ("●" if glow else Glyphs.DOT) + "[/]"
        status_label = f"[{color}]{'▸' if glow else status[:4]}[/]"
        # Per-agent tokens/cost inline
        meta_parts: list[str] = []
        if info.total_tokens:
            meta_parts.append(f"{info.total_tokens:,} tok")
        if info.cost_usd:
            meta_parts.append(f"${info.cost_usd:.2f}")
        if info.ctx_limit and info.ctx_used:
            pct = int(info.ctx_used / max(1, info.ctx_limit) * 100)
            meta_parts.append(f"{pct}% ctx")
        meta = f" [{Tokens.TEXT_MUTED}]{' · '.join(meta_parts)}[/]" if meta_parts else ""
        line = f"{dot} [{Tokens.TEXT}]{escape(name)}[/] {status_label} [{Tokens.TEXT_DIM}]{escape(task)}[/]{meta}"
        if status in ("done", "cancelled"):
            line = f"[{Styles.DE_EMPHASIS}]{line}[/]"

        node = parent_node.add(line)
        # Expand full prompt underneath when present and not overly long
        if info.prompt and info.prompt != info.task:
            prompt_preview = escape(info.prompt[:80].replace("\n", " "))
            node.add(f"[{Tokens.TEXT_MUTED}]↳ {prompt_preview}[/]")
        children = [a for a in agents if a.parent_id == aid]
        for c in sorted(children, key=lambda x: x.name):
            if len(seen) >= VIRTUAL_LIMIT:
                break
            add_node(node, c.id, depth + 1)

    root_ids = [a.id for a in agents if a.parent_id is None]
    for rid in root_ids:
        if len(seen) >= VIRTUAL_LIMIT:
            break
        add_node(tree, rid)
    if len(agents) > VIRTUAL_LIMIT:
        tree.add(f"[{Tokens.TEXT_MUTED}]… +{len(agents) - VIRTUAL_LIMIT} more (scroll to view)[ /]")

    return tree


def _task_row(icon: str, color: str, task_id: int, title: str, priority: str) -> str:
    pri = ""
    if priority == "high":
        pri = f" [{Tokens.DANGER}]![/]"
    return (
        f"[{color}]{icon}[/] [{Tokens.TEXT_MUTED}]{task_id}.[/] "
        f"[{Tokens.TEXT}]{escape(title)}[/]{pri}"
    )


def render_tasks(s: SessionState) -> RenderableType:
    """Return markup for the TODO checklist pane.

    Title handles both /tasks alias and TODOS; empty state no longer leaks
    manage_tasks internals and offers add/edit/delete affordance.
    """
    # Consistent title for both TODOS and /tasks alias
    title = f"[{Styles.SECTION}]TASKS[/]  [{Tokens.TEXT_MUTED}]· TODOS · /tasks[/]"
    if not s.current_tasks:
        return (
            f"{title}\n\n"
            f"[{Tokens.TEXT_MUTED}](no tasks yet)[/]\n"
            f"[{Tokens.TEXT_MUTED}]Use [/][{Tokens.ACCENT}]Add Task[/][{Tokens.TEXT_MUTED}] below or [/][{Tokens.TEXT}]manage_tasks[/][{Tokens.TEXT_MUTED}] tool[/]\n"
            f"[{Tokens.TEXT_MUTED}]Press [/][{Tokens.TEXT}]a[/][{Tokens.TEXT_MUTED}] to add · [/][{Tokens.TEXT}]e[/][{Tokens.TEXT_MUTED}] edit · [/][{Tokens.TEXT}]d[/][{Tokens.TEXT_MUTED}] delete[/]"
        )

    tasks = s.current_tasks
    summary = str(tasks.get("summary") or "")
    head = f"[{Tokens.TEXT_MUTED}]{escape(summary)}[/]" if summary else ""

    lines: list[str] = [title]
    if head:
        lines.append(head)

    in_progress = tasks.get("inProgress") or []
    pending = tasks.get("pending") or []
    completed = tasks.get("completed") or []

    if in_progress:
        lines.append(f"\n[{Tokens.AGENT}]In progress[/]")
        for t in in_progress:
            lines.append(
                _task_row(
                    "▸",
                    Tokens.AGENT,
                    int(t.get("id", 0)),
                    str(t.get("title", "")),
                    str(t.get("priority", "")),
                )
            )

    if pending:
        lines.append(f"\n[{Tokens.TEXT_DIM}]Pending[/]")
        for t in pending:
            lines.append(
                _task_row(
                    "·",
                    Tokens.TEXT_MUTED,
                    int(t.get("id", 0)),
                    str(t.get("title", "")),
                    str(t.get("priority", "")),
                )
            )

    if completed:
        lines.append(f"\n[{Tokens.TEXT_MUTED}]Completed[/]")
        for t in completed:
            lines.append(
                f"[{Styles.DE_EMPHASIS}]{_task_row(Glyphs.TOOL_OK, Tokens.TEXT_MUTED, int(t.get('id', 0)), str(t.get('title', '')), str(t.get('priority', '')))}[/]"
            )

    if not in_progress and not pending and not completed:
        lines.append(f"\n[{Tokens.TEXT_MUTED}](empty list — press a to add)[/]")

    # Sticky section header hint + virtualized scroll: cap visible tasks
    total_tasks = len(in_progress) + len(pending) + len(completed)
    if total_tasks > 30:
        lines.append(
            f"\n[{Tokens.TEXT_MUTED}]… {total_tasks} tasks (scroll for more, sticky headers)[ /]"
        )
    else:
        lines.append(f"\n[{Tokens.TEXT_MUTED}]a add · e edit · d delete · /tasks[/]")

    return "\n".join(lines)


def render_composer_context(s: SessionState) -> str:
    """Markup for the composer context chips + live ctx gauge.

    Shows pinned files as muted chips (truncated to 4 + overflow) and a
    compact ctx usage bar. Always rendered so the composer never looks
    empty — when no files are pinned a hint is shown instead.
    """
    files = s.context_files or []
    parts: list[str] = []

    # Pinned file chips — each chip shows an interactive × (explain /unpin)
    if files:
        MAX_SHOW = 4
        chips: list[str] = []
        for f in files[:MAX_SHOW]:
            raw = str(f.get("path") or "")
            disp = escape(raw)
            if len(disp) > 28:
                disp = disp[:12] + "…" + disp[-15:]
            # × affordance: clicking is not wired yet, but the hint matches /unpin
            chips.append(f"[{Tokens.TEXT}]{disp}[/] [{Tokens.TEXT_MUTED}]×[/]")
        line = f"[{Tokens.TEXT_MUTED}]pinned[/] " + f" [{Tokens.TEXT_MUTED}]·[/] ".join(chips)
        if len(files) > MAX_SHOW:
            line += f" [{Tokens.TEXT_MUTED}]+{len(files) - MAX_SHOW} more[/]"
        line += f"  [{Tokens.TEXT_MUTED}]({len(files)} · /unpin)[/]"
        parts.append(line)
    else:
        parts.append(f"[{Tokens.TEXT_MUTED}]no pinned files — @ to pin · /pin <path> · /context[/]")

    # Compact ctx gauge (always visible when limit known)
    if s.ctx_limit:
        ctx_ratio = s.ctx_used / max(1, s.ctx_limit)
        if ctx_ratio >= 0.9:
            ctx_color = Tokens.DANGER
        elif ctx_ratio >= 0.8:
            ctx_color = Tokens.WARN
        else:
            ctx_color = Tokens.TEXT
        w = 8
        f_count = min(w, max(0, int(ctx_ratio * w)))
        bar = (
            f"[{ctx_color}]"
            + ("█" * f_count)
            + "[/]"
            + f"[{Tokens.LINE}]"
            + ("░" * (w - f_count))
            + "[/]"
        )
        used_s = f"{s.ctx_used:,}" if s.ctx_used else "0"
        lim_s = f"{s.ctx_limit // 1000}k" if s.ctx_limit else "?"
        pct = int(ctx_ratio * 100)
        parts.append(f"[{Tokens.TEXT_MUTED}]ctx[/] [{ctx_color}]{used_s}/{lim_s} {pct}%[/] {bar}")

        if s.budget_usd and s.budget_usd > 0:
            b_ratio = min(1.0, s.cost_usd / s.budget_usd) if s.budget_usd else 0
            bf = min(w, max(0, int(b_ratio * w)))
            b_bar = (
                f"[{Tokens.TEXT_DIM}]"
                + ("█" * bf)
                + "[/]"
                + f"[{Tokens.LINE}]"
                + ("░" * (w - bf))
                + "[/]"
            )
            parts.append(
                f"[{Tokens.TEXT_MUTED}]$[/] [{Tokens.TEXT_DIM}]${s.cost_usd:.2f}/${s.budget_usd:.2f}[/] {b_bar}"
            )

    return "  ".join(parts) if parts else ""


def estimate_tokens(text: str) -> int:
    """Rough word-level token estimate for the composer counter.

    Uses ~1.3 tokens per word + char fallback so short punctuation-heavy
    inputs still show a non-zero count without importing tiktoken.
    """
    if not text or not text.strip():
        return 0
    words = len(text.split())
    # 1.3x words, plus char-based floor for code/punctuation
    return max(words, int(words * 1.3 + len(text) / 12))


def composer_token_counter(text: str, limit: int | None = None) -> str:
    """Markup for the word-level token counter under the composer."""
    n = estimate_tokens(text)
    w = f"{n:,}"
    if limit and limit > 0:
        pct = min(100, int(n / max(1, limit) * 100))
        color = Tokens.DANGER if pct >= 90 else Tokens.WARN if pct >= 75 else Tokens.TEXT_MUTED
        return f"[{color}]{w} tok {pct}%[/]"
    return f"[{Tokens.TEXT_MUTED}]{w} tok[/]"


def composer_footer_markup(s: SessionState, width: int | None = None) -> str:
    """Return markup for the composer footer bar.

    When ``width`` is narrow, the hints collapse to a short form so the
    footer never wraps into a second row.
    """
    reasoning = s.reasoning or "none"
    full_hints = composer_footer_hints()
    # Responsive collapse: below 90 cols show only send/newline + palette
    if width is not None and width < 90:
        short = f"↵ send · ⇧↵ newline · {s.model or '…'}"
        hints_text = short
    elif width is not None and width < 110:
        # mid width: drop history + mention
        hints_text = f"↵ send · ⇧↵ newline · / commands · {s.model or ''}".strip()
        if not hints_text:
            hints_text = full_hints
    else:
        hints_text = full_hints
    hints = f"[{Tokens.TEXT_MUTED}]{escape(hints_text)}[/]"
    meta = f"[{Tokens.TEXT_DIM}]reasoning:[/] [{Tokens.THOUGHT}]{escape(reasoning)}[/]"
    if not s.ready:
        return f"[{Tokens.TEXT_MUTED}]Waiting for agent…[/]   {hints}   {meta}"
    if s.progress:
        prog = s.progress
        label = escape(str(prog.get("label") or "Working"))
        current = prog.get("current")
        total = prog.get("total")
        if current is not None and total is not None:
            progress_label = f"[{Tokens.AGENT}]{label}[/] [{Tokens.TEXT_DIM}]{current}/{total}[/]"
        else:
            progress_label = f"[{Tokens.AGENT}]{label}[/]"
        return f"{progress_label}   {hints}   {meta}"
    return f"{hints}   {meta}"
