"""Rendering helpers extracted from CoderAIApp.

Each function takes the minimal state it needs and returns a markup
string.  CoderAIApp methods delegate to these.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from coderAI.tui.platform import composer_footer_hints, header_palette_hint
from coderAI.tui.state import SessionState
from coderAI.tui.theme import Glyphs, Styles, Tokens


def render_session_header(s: SessionState) -> str:
    """Return markup for the top session header bar."""
    status_color = Tokens.AGENT if s.streaming or s.thinking else Tokens.TEXT_DIM
    ctx_used = f"{s.ctx_used:,}" if s.ctx_used else "0"
    ctx_lim = f"{s.ctx_limit // 1000}k" if s.ctx_limit else "?"
    model_label = s.model or "…"
    provider = s.provider or ""

    def chip(label: str, value: str, color: str = Tokens.TEXT, bar: float = -1) -> str:
        inner = f"[{Tokens.TEXT_MUTED}]{label}[/] [{color}]{value}[/]"
        if bar >= 0:
            w = 10
            f = min(w, max(0, int(bar * w)))
            b = f"[{color}]" + ("█" * f) + "[/]"
            b += f"[{Tokens.LINE}]" + ("─" * (w - f)) + "[/]"
            inner += f" {b}"
        return inner

    ctx_ratio = (s.ctx_used / max(1, s.ctx_limit)) if s.ctx_limit else 0
    budget_ratio = (s.cost_usd / s.budget_usd) if s.budget_usd and s.budget_usd > 0 else -1
    cost_val = (
        f"${s.cost_usd:.4f} / ${s.budget_usd:.2f}"
        if s.budget_usd and s.budget_usd > 0
        else f"${s.cost_usd:.4f}"
    )

    chips = [
        f"[{status_color}]{Glyphs.DOT}[/] [{Tokens.TEXT}]{model_label}[/]",
        chip("provider", provider, Tokens.TEXT_MUTED),
        chip("ctx", f"{ctx_used} / {ctx_lim}", Tokens.TEXT, bar=ctx_ratio),
        chip("$", cost_val, Tokens.TEXT_DIM, bar=budget_ratio),
        chip("iter", f"{s.iteration}/{s.max_iterations}", Tokens.TEXT_DIM),
    ]
    if s.elapsed_s > 0:
        m, sec = divmod(int(s.elapsed_s), 60)
        ts = f"{m}m {sec}s" if m > 0 else f"{sec}s"
        chips.append(chip("t", ts, Tokens.TEXT_MUTED))
    active = sum(1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled"))
    if active:
        chips.append(chip("agents", f"{active} active", Tokens.AGENT))
    yolo_c = Tokens.WARN if s.auto_approve else Tokens.TEXT_MUTED
    yolo_v = "on" if s.auto_approve else "off"
    chips.append(chip("yolo", yolo_v, yolo_c))
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
    hints = f"[{Tokens.TEXT_MUTED}]{header_palette_hint()}[/]"
    return f"{left}\n{hints}"


def render_agent_tree(s: SessionState) -> RenderableType:
    """Return a compact Rich Tree for the left agent panel."""
    active_count = sum(
        1 for a in s.agents.values() if a.status not in ("done", "error", "cancelled")
    )
    title = f"[{Styles.SECTION}]AGENTS[/]  [{Tokens.TEXT_MUTED}]· {active_count} active[/]"
    if not s.agents:
        return title + f"\n[{Tokens.TEXT_MUTED}](no agents yet)[/]"

    tree = Tree(title, guide_style=Tokens.LINE)
    agents = list(s.agents.values())
    seen: set[str] = set()

    def add_node(parent_node: Tree, aid: str) -> None:
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
        task = (info.task or "")[:24]
        dot = f"[{color}]" + ("●" if glow else Glyphs.DOT) + "[/]"
        status_label = f"[{color}]{'▸' if glow else status[:4]}[/]"
        line = f"{dot} [{Tokens.TEXT}]{name}[/] {status_label} [{Tokens.TEXT_DIM}]{task}[/]"
        if status in ("done", "cancelled"):
            line = f"[dim]{line}[/]"

        node = parent_node.add(line)
        children = [a for a in agents if a.parent_id == aid]
        for c in sorted(children, key=lambda x: x.name):
            add_node(node, c.id)

    root_ids = [a.id for a in agents if a.parent_id is None]
    for rid in root_ids:
        add_node(tree, rid)

    return tree


def render_plan(s: SessionState) -> RenderableType:
    """Return a rich Table for the current plan pane."""
    title = f"[{Styles.SECTION}]CURRENT PLAN[/]"
    if not s.current_plan:
        return f"{title}\n\n[{Tokens.TEXT_MUTED}](no active plan)[/]"

    plan = s.current_plan
    p_title = str(plan.get("title") or "")
    completed = int(plan.get("completed") or 0)
    total = int(plan.get("total") or 0)
    current = int(plan.get("currentIdx") or 0)
    steps = plan.get("steps") or []

    head = f"[{Tokens.TEXT}]{escape(p_title)}[/]"
    if total:
        head += f" [{Tokens.TEXT_MUTED}]· {completed}/{total}[/]"

    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1, 0, 0))
    table.add_column("Icon", justify="right")
    table.add_column("Index", style=Tokens.TEXT_MUTED)
    table.add_column("Description")

    for s_obj in steps:
        idx = int(s_obj.get("index", 0))
        status = str(s_obj.get("status", "pending"))
        desc = escape(str(s_obj.get("description", "")))
        if status == "done":
            g, c = Glyphs.TOOL_OK, Tokens.AGENT
        elif idx == current + 1:
            g, c = "▸", Tokens.WARN
        else:
            g, c = "·", Tokens.TEXT_MUTED

        table.add_row(
            f"[{c}]{g}[/]",
            f"{idx}.",
            f"[{Tokens.TEXT}]{desc}[/]",
        )

    return Group(f"{title}\n{head}\n", table)


def _task_row(icon: str, color: str, task_id: int, title: str, priority: str) -> str:
    pri = ""
    if priority == "high":
        pri = f" [{Tokens.DANGER}]![/]"
    return (
        f"[{color}]{icon}[/] [{Tokens.TEXT_MUTED}]{task_id}.[/] "
        f"[{Tokens.TEXT}]{escape(title)}[/]{pri}"
    )


def render_tasks(s: SessionState) -> RenderableType:
    """Return markup for the TODO checklist pane."""
    title = f"[{Styles.SECTION}]TODOS[/]"
    if not s.current_tasks:
        return f"{title}\n\n[{Tokens.TEXT_MUTED}](no tasks — agent can add with manage_tasks)[/]"

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
        lines.append("\n[dim]Completed[/]")
        for t in completed:
            lines.append(
                f"[dim]{_task_row(Glyphs.TOOL_OK, Tokens.TEXT_MUTED, int(t.get('id', 0)), str(t.get('title', '')), str(t.get('priority', '')))}[/]"
            )

    if not in_progress and not pending and not completed:
        lines.append(f"\n[{Tokens.TEXT_MUTED}](empty list)[/]")

    return "\n".join(lines)


def composer_footer_markup(s: SessionState) -> str:
    """Return markup for the composer footer bar."""
    reasoning = s.reasoning or "none"
    hints = f"[{Tokens.TEXT_MUTED}]{composer_footer_hints()}[/]"
    meta = f"[{Tokens.TEXT_DIM}]reasoning:[/] [{Tokens.THOUGHT}]{reasoning}[/]"
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
