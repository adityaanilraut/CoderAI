"""Approval modal panel for tool and permission confirmation.

Groups same-file diff previews truncated to MAX_PREVIEW_LINES=4,
displays styled approval options, and supports interactive paging.
"""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.padding import Padding
from rich.text import Text

try:
    from coderai.cli.console import console  # type: ignore
except Exception:
    from rich.console import Console

    console = Console()  # type: ignore

MAX_PREVIEW_LINES = 4


def _render_feedback_with_cursor(text: str, cursor: int | None) -> Text:
    if cursor is None or cursor >= len(text):
        return Text(text + "\u2588")
    cursor = max(cursor, 0)
    return Text.assemble(
        Text(text[:cursor]), Text(text[cursor], style="reverse"), Text(text[cursor + 1 :])
    )


class ApprovalRequestPanel:
    """Per-request approval panel for CoderAI dict requests."""

    FEEDBACK_OPTION_INDEX = 3
    modal_priority = 20

    def __init__(self, request: dict[str, Any]):
        self.request = request
        self._preview_renderables: list[RenderableType] = []
        self._has_diff = False
        self._non_diff_truncated = False
        self._content_blocks: list[dict[str, Any]] = []

        command = str(request.get("command", "")).strip()
        scopes: list[str] = request.get("scopes") or []
        diff_preview = request.get("diff_preview")
        description = request.get("description", "")

        # Determine always target — duplicate set to avoid circular import (ponytail)
        _ALWAYS = {
            "read-in-cwd",
            "read-out-cwd",
            "write-in-cwd",
            "write-out-cwd",
            "delete-in-cwd",
            "delete-out-cwd",
            "query-git-log",
            "mutate-git-log",
            "network",
            "mcp",
        }
        always_target = next((s for s in scopes if s in _ALWAYS), None)
        has_always = bool(always_target)

        _SCOPE_DESC = {
            "read-in-cwd": "reads inside this workspace",
            "read-out-cwd": "reads outside this workspace",
            "write-in-cwd": "writes inside this workspace",
            "write-out-cwd": "writes outside this workspace",
            "delete-in-cwd": "deletes inside this workspace",
            "delete-out-cwd": "deletes outside this workspace",
            "query-git-log": "Git history queries",
            "mutate-git-log": "Git history changes",
            "network": "network access",
            "mcp": "MCP tool access",
        }
        # Build options
        self.options: list[tuple[str, str]] = [("Approve once", "approve")]
        if has_always and always_target:
            label = f"Approve for session ({_SCOPE_DESC.get(always_target, always_target)})"
            self.options.append((label, "approve_for_session"))
            self.options.append(("Reject", "reject"))
            self.options.append(("Reject, tell the model what to do instead", "reject"))
        else:
            self.options.append(("Reject", "reject"))
            # still keep feedback option for parity (3 options + feedback)
            # Kimi always has 4 options; for has_always=False we show 3 + feedback = 4 but second is reject?
            # To keep 4 entries, insert approve_for_session only when has_always; otherwise 3 entries but FEEDBACK index 2?
            # For consistency with Kimi 4-options, pad if needed:
            if len(self.options) == 2:
                # options are [approve, reject] -> add feedback as 3rd (index 2)
                self.options.append(("Reject, tell the model what to do instead", "reject"))
                self.FEEDBACK_OPTION_INDEX = 2  # type: ignore
            else:
                self.FEEDBACK_OPTION_INDEX = 3
        # ensure index valid
        if len(self.options) <= self.FEEDBACK_OPTION_INDEX:
            self.FEEDBACK_OPTION_INDEX = len(self.options) - 1

        self.selected_index = 0

        # Build preview renderables
        # 1. Description if no diff
        # 2. Diff grouping: same-file hunks MAX_PREVIEW_LINES=4 (Kimi collect_diff_hunks parity)
        if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
            self._has_diff = True
            try:
                from coderai.cli.diff_render import (
                    parse_unified_diff_to_hunks,
                    render_diff_preview_structured,
                )

                hunks, added, removed, path = parse_unified_diff_to_hunks(diff_preview)
                if hunks:
                    # Group same-file: hunks already grouped; render structured preview limited to 4
                    renderables, remaining = render_diff_preview_structured(
                        path, hunks, added, removed, max_lines=MAX_PREVIEW_LINES
                    )
                    self._preview_renderables.extend(renderables)
                    if remaining > 0 or len(diff_preview.splitlines()) > MAX_PREVIEW_LINES:
                        self._non_diff_truncated = False
                        self.has_expandable_content = True
                    else:
                        self.has_expandable_content = (
                            len(diff_preview.splitlines()) > MAX_PREVIEW_LINES
                        )
                    # Keep full content for pager
                    self._content_blocks.append({"diff_preview": diff_preview, "path": path})
                else:
                    # Fallback truncated text preview
                    lines = diff_preview.strip().splitlines()[:MAX_PREVIEW_LINES]
                    self._preview_renderables.append(Text("\n".join(lines)))
                    self.has_expandable_content = len(diff_preview.splitlines()) > MAX_PREVIEW_LINES
                    self._content_blocks.append({"diff_preview": diff_preview})
            except Exception:
                lines = diff_preview.strip().splitlines()[:MAX_PREVIEW_LINES]
                self._preview_renderables.append(Text("\n".join(lines)))
                self.has_expandable_content = len(diff_preview.splitlines()) > MAX_PREVIEW_LINES
        else:
            # Non-diff content: command + description
            combined = ""
            if command:
                combined = command
            if description:
                combined = f"{combined}\n{description}" if combined else description
            if combined:
                lines = combined.strip().splitlines()
                truncated = "\n".join(lines[:MAX_PREVIEW_LINES])
                self._preview_renderables.append(Text(truncated))
                if len(lines) > MAX_PREVIEW_LINES:
                    self._non_diff_truncated = True
                    self.has_expandable_content = True
                else:
                    self.has_expandable_content = False
            else:
                # scopes only
                self._preview_renderables.append(
                    Text(f"Scopes: {', '.join(scopes) or 'none'}", style="grey50")
                )
                self.has_expandable_content = False
        # ensure attribute exists
        if not hasattr(self, "has_expandable_content"):
            self.has_expandable_content = self._has_diff or self._non_diff_truncated

    def render(
        self, *, feedback_text: str | None = None, feedback_cursor: int | None = None
    ) -> RenderableType:
        req = self.request
        name = str(req.get("name", "Tool")).lower()
        command = str(req.get("command", "")).strip()
        cwd = req.get("cwd") or req.get("project_root") or ""
        description = req.get("description", "")

        # Action title matching screenshot
        if "bash" in name or "terminal" in name or command:
            title_text = "Run this command?"
        elif "write" in name or "edit" in name or "patch" in name:
            target = req.get("file_path") or req.get("target_path") or "file"
            title_text = f"Edit {target}?" if "edit" in name else f"Write to {target}?"
        else:
            title_text = f"Execute {req.get('name', 'action')}?"

        # Header with amber/orange arrow and horizontal rule across width
        header = Text()
        header.append("▶ ", style="bold #f59e0b")
        header.append(f"{title_text} ", style="bold #f59e0b")
        header_len = len(header.plain)
        rule_len = max(10, (console.width if getattr(console, "width", 0) else 80) - header_len - 2)
        header.append("─" * rule_len, style="#d97706")

        content_lines: list[RenderableType] = [header]

        # Context details
        if cwd:
            content_lines.append(Text(f"  cwd: {cwd}", style="dim"))
        if command:
            cmd_text = Text()
            cmd_text.append("  $ ", style="dim")
            cmd_text.append(command, style="bold cyan")
            content_lines.append(cmd_text)
        if description and description != command:
            content_lines.append(Text(f"    {description.strip()}", style="dim"))

        # Previews (diffs, etc.)
        if self._preview_renderables:
            content_lines.append(Text(""))
            for r in self._preview_renderables:
                content_lines.append(Padding(r, (0, 0, 0, 2)))

        if self.has_expandable_content and self._non_diff_truncated:
            content_lines.append(Text("  ... (truncated, ctrl-e to expand)", style="dim italic"))

        # Menu options
        show_inline_feedback = feedback_text is not None and self.is_feedback_selected
        content_lines.append(Text(""))

        for i, (option_text, _) in enumerate(self.options):
            num = i + 1
            is_feedback = i == self.FEEDBACK_OPTION_INDEX
            if i == self.selected_index:
                if is_feedback and show_inline_feedback:
                    inp = _render_feedback_with_cursor(feedback_text or "", feedback_cursor)
                    opt_line = Text.assemble(Text(f"▶ {num}. Reject: "), inp, style="bold cyan")
                    content_lines.append(opt_line)
                else:
                    content_lines.append(Text(f"▶ {num}. {option_text}", style="bold cyan"))
            else:
                content_lines.append(Text(f"  {num}. {option_text}", style="white"))

        # Footer hint bar
        content_lines.append(Text(""))
        if show_inline_feedback:
            hint = "  Type your feedback, then press Enter to submit."
        else:
            hint = "  ↑/↓ select · 1/2/3/4 choose · ↵ confirm"
            if self.has_expandable_content:
                hint += " · ctrl-e expand"
        content_lines.append(Text(hint, style="dim"))

        return Group(*content_lines)

    def render_full(self) -> list[RenderableType]:
        out: list[RenderableType] = []
        for cb in self._content_blocks:
            dp = cb.get("diff_preview")
            if dp:
                out.append(Text(dp))
        return out

    def move_up(self) -> None:
        self.selected_index = (self.selected_index - 1) % len(self.options)

    def move_down(self) -> None:
        self.selected_index = (self.selected_index + 1) % len(self.options)

    @property
    def is_feedback_selected(self) -> bool:
        return self.selected_index == self.FEEDBACK_OPTION_INDEX

    def get_selected_response(self) -> str:
        return self.options[self.selected_index][1]


def show_approval_in_pager(panel: ApprovalRequestPanel) -> None:
    """Show full approval content in pager (console.screen()+pager)."""
    with console.screen(), console.pager(styles=True):
        req = panel.request
        sender = req.get("name", "Agent")
        action = req.get("command") or req.get("name", "action")
        console.print(
            Text.from_markup(
                f"[yellow]⚠ {escape(str(sender))} is requesting approval to {escape(str(action))}:[/yellow]"
            )
        )
        console.print()
        # Render full diff via structured panel if available
        diff_preview = req.get("diff_preview")
        if diff_preview and isinstance(diff_preview, str) and diff_preview.strip():
            try:
                from coderai.cli.diff_render import parse_unified_diff_to_hunks, render_diff_panel

                hunks, added, removed, path = parse_unified_diff_to_hunks(diff_preview)
                if hunks:
                    console.print(render_diff_panel(path, hunks, added, removed))
                else:
                    console.print(Text(diff_preview))
            except Exception:
                console.print(Text(diff_preview))
        else:
            for r in panel.render_full():
                console.print(r)
