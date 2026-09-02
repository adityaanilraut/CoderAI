"""Question modal panel — Phase4 lean port of Kimi _question_panel.py:24-586.

Tabs ●/✓/○, ? yellow, → [n] cyan, multi [✓] Space, Other inline,
modal_priority=10, _saved_selections. ponytail: dict-based questions
(CoderAI AskUserQuestion payload) — no prompt_toolkit delegate duplication;
app.py holds simple input loop fallback (KeyboardListener hook is thin).
"""

from __future__ import annotations

from typing import Any

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

try:
    from coderai.cli.console import console
except Exception:
    from rich.console import Console

    console = Console()  # type: ignore

try:
    from rich.markdown import Markdown  # type: ignore
except Exception:
    Markdown = None  # type: ignore

OTHER_OPTION_LABEL = "Other"


class QuestionRequestPanel:
    """Renders structured questions — tabs + multi-select + Other inline."""

    modal_priority = 10

    def __init__(self, questions: list[dict[str, Any]]):
        # Normalize to internal list of dicts with keys: question, options, multiSelect, header, body
        self.questions: list[dict[str, Any]] = []
        for q in questions:
            qd = dict(q)
            # Ensure options list of dicts with label/description
            opts = qd.get("options") or []
            norm_opts: list[dict[str, Any]] = []
            for o in opts:
                if isinstance(o, dict):
                    norm_opts.append(
                        {"label": o.get("label", ""), "description": o.get("description", "")}
                    )
                elif isinstance(o, (list, tuple)) and len(o) >= 1:
                    norm_opts.append(
                        {"label": str(o[0]), "description": str(o[1]) if len(o) > 1 else ""}
                    )
                else:
                    norm_opts.append({"label": str(o), "description": ""})
            qd["options"] = norm_opts
            self.questions.append(qd)

        self._current_question_index = 0
        self._answers: dict[str, str] = {}
        self._saved_selections: dict[int, tuple[int, set[int]]] = {}
        self._other_drafts: dict[int, str] = {}
        self._selected_index = 0
        self._multi_selected: set[int] = set()
        self._body_text: str = ""
        self.has_expandable_content: bool = False
        self._options: list[tuple[str, str]] = []
        self._setup_current_question()

    # -- setup ---------------------------------------------------------------
    def _setup_current_question(self) -> None:
        q = self._current_question
        # options as tuples (label, desc)
        self._options = [(o["label"], o.get("description", "")) for o in q.get("options", [])]
        other_label = q.get("other_label") or q.get("otherLabel") or OTHER_OPTION_LABEL
        other_desc = q.get("other_description") or q.get("otherDescription") or ""
        self._options.append((other_label, other_desc))
        idx = self._current_question_index
        if idx in self._saved_selections:
            saved_idx, saved_multi = self._saved_selections[idx]
            self._selected_index = min(saved_idx, len(self._options) - 1)
            self._multi_selected = set(saved_multi)
        elif q.get("question") in self._answers:
            answer = self._answers[q["question"]]
            if q.get("multiSelect"):
                answer_labels = [a.strip() for a in answer.split(",")]
                known = {label for label, _ in self._options[:-1]}
                self._multi_selected = set()
                for i, (label, _) in enumerate(self._options[:-1]):
                    if label in answer_labels:
                        self._multi_selected.add(i)
                if any(al not in known for al in answer_labels):
                    self._multi_selected.add(len(self._options) - 1)
                self._selected_index = min(self._multi_selected) if self._multi_selected else 0
            else:
                for i, (label, _) in enumerate(self._options):
                    if label == answer:
                        self._selected_index = i
                        break
                else:
                    self._selected_index = len(self._options) - 1
                self._multi_selected = set()
        else:
            self._selected_index = 0
            self._multi_selected = set()
        self._recompute_body()

    def _recompute_body(self) -> None:
        body = self._current_question.get("body") or self._current_question.get("description") or ""
        self._body_text = body.rstrip("\n") if body else ""
        self.has_expandable_content = bool(self._body_text)

    @property
    def _current_question(self) -> dict[str, Any]:
        return self.questions[self._current_question_index]

    @property
    def is_other_selected(self) -> bool:
        return self._selected_index == len(self._options) - 1

    @property
    def is_multi_select(self) -> bool:
        return bool(self._current_question.get("multiSelect"))

    @property
    def current_question_text(self) -> str:
        return self._current_question.get("question", "")

    def should_prompt_other_input(self) -> bool:
        if not self.is_multi_select:
            return self.is_other_selected
        other_idx = len(self._options) - 1
        return other_idx in self._multi_selected

    def select_index(self, index: int) -> bool:
        if not 0 <= index < len(self._options):
            return False
        self._selected_index = index
        return True

    # -- render --------------------------------------------------------------
    def render(self, *, other_input_text: str | None = None) -> RenderableType:
        q = self._current_question
        lines: list[RenderableType] = []
        if len(self.questions) > 1:
            tab_parts: list[str] = []
            for i, qi in enumerate(self.questions):
                label = escape(qi.get("header") or f"Q{i + 1}")
                if i == self._current_question_index:
                    icon, style = "●", "bold cyan"
                elif qi.get("question") in self._answers:
                    icon, style = "✓", "green"
                else:
                    icon, style = "○", "grey50"
                tab_parts.append(f"[{style}]({icon}) {label}[/{style}]")
            lines.append(Text.from_markup("  ".join(tab_parts)))
            lines.append(Text(""))
        lines.append(Text.from_markup(f"[yellow]? {escape(q.get('question', ''))}[/yellow]"))
        if q.get("multiSelect"):
            lines.append(Text("  (SPACE to toggle, ENTER to submit)", style="dim italic"))
        lines.append(Text(""))
        if self._body_text:
            lines.append(
                Text.from_markup("  [bold cyan]  ▶ Press ctrl-e to view full content[/bold cyan]")
            )
            lines.append(Text(""))
        show_inline = other_input_text is not None and self.is_other_selected
        for i, (label, desc) in enumerate(self._options):
            num = i + 1
            is_other = i == len(self._options) - 1
            if q.get("multiSelect"):
                checked = "✓" if i in self._multi_selected else " "
                prefix = f"[{checked}]"
                if i == self._selected_index:
                    lines.append(Text.from_markup(f"[cyan]{prefix} {escape(label)}[/cyan]"))
                else:
                    lines.append(Text.from_markup(f"[grey50]{prefix} {escape(label)}[/grey50]"))
            else:
                if i == self._selected_index:
                    if is_other and show_inline:
                        inp = escape(other_input_text) if other_input_text else ""
                        lines.append(
                            Text.from_markup(f"[cyan]→ \\[{num}] {escape(label)}: {inp}█[/cyan]")
                        )
                    else:
                        lines.append(Text.from_markup(f"[cyan]→ \\[{num}] {escape(label)}[/cyan]"))
                else:
                    lines.append(Text.from_markup(f"[grey50]  \\[{num}] {escape(label)}[/grey50]"))
            if desc and not (is_other and show_inline):
                lines.append(Text(f"      {desc}", style="dim"))
        if show_inline:
            lines.append(Text(""))
            lines.append(
                Text("  Type your answer, then press Enter to submit.", style="dim italic")
            )
        elif len(self.questions) > 1:
            lines.append(Text(""))
            lines.append(Text("  ◄/► switch question  ▲/▼ select  ↵ submit  esc exit", style="dim"))
        return Panel(
            Group(*lines),
            border_style="grey50",
            title="[bold]question[/bold]",
            title_align="left",
            padding=(0, 1),
        )

    # -- drafts --------------------------------------------------------------
    def save_other_draft(self, text: str) -> None:
        if text:
            self._other_drafts[self._current_question_index] = text
        else:
            self._other_drafts.pop(self._current_question_index, None)

    def get_other_draft(self) -> str:
        return self._other_drafts.get(self._current_question_index, "")

    def go_to(self, index: int) -> None:
        if index == self._current_question_index or not 0 <= index < len(self.questions):
            return
        self._saved_selections[self._current_question_index] = (
            self._selected_index,
            set(self._multi_selected),
        )
        self._current_question_index = index
        self._setup_current_question()

    def next_tab(self) -> None:
        if self._current_question_index < len(self.questions) - 1:
            self.go_to(self._current_question_index + 1)

    def prev_tab(self) -> None:
        if self._current_question_index > 0:
            self.go_to(self._current_question_index - 1)

    def move_up(self) -> None:
        self._selected_index = (self._selected_index - 1) % len(self._options)

    def move_down(self) -> None:
        self._selected_index = (self._selected_index + 1) % len(self._options)

    def toggle_select(self) -> None:
        if not self.is_multi_select:
            return
        if self._selected_index in self._multi_selected:
            self._multi_selected.discard(self._selected_index)
        else:
            self._multi_selected.add(self._selected_index)

    # -- submit --------------------------------------------------------------
    def submit(self) -> bool:
        q = self._current_question
        if q.get("multiSelect"):
            other_idx = len(self._options) - 1
            if other_idx in self._multi_selected:
                return False
            labels = [
                self._options[i][0]
                for i in sorted(self._multi_selected)
                if i < len(q.get("options", []))
            ]
            if not labels:
                return False
            self._answers[q["question"]] = ", ".join(labels)
        else:
            if self.is_other_selected:
                return False
            self._answers[q["question"]] = self._options[self._selected_index][0]
        self._saved_selections.pop(self._current_question_index, None)
        self._other_drafts.pop(self._current_question_index, None)
        return self._advance()

    def submit_other(self, text: str) -> bool:
        q = self._current_question
        if q.get("multiSelect"):
            other_idx = len(self._options) - 1
            labels = [
                self._options[i][0]
                for i in sorted(self._multi_selected)
                if i < len(q.get("options", [])) and i != other_idx
            ]
            if text:
                labels.append(text)
            self._answers[q["question"]] = ", ".join(labels) if labels else text
        else:
            self._answers[q["question"]] = text
        self._saved_selections.pop(self._current_question_index, None)
        self._other_drafts.pop(self._current_question_index, None)
        return self._advance()

    def _advance(self) -> bool:
        total = len(self.questions)
        if len(self._answers) >= total:
            return True
        for offset in range(1, total + 1):
            idx = (self._current_question_index + offset) % total
            if self.questions[idx].get("question") not in self._answers:
                self._current_question_index = idx
                self._setup_current_question()
                return False
        return True

    def get_answers(self) -> dict[str, str]:
        return dict(self._answers)

    def render_full_body(self) -> list[RenderableType]:
        if not self._body_text or Markdown is None:
            return [Text(self._body_text)] if self._body_text else []
        try:
            return [Markdown(self._body_text)]
        except Exception:
            return [Text(self._body_text)]


def show_question_body_in_pager(panel: QuestionRequestPanel) -> None:
    with console.screen(), console.pager(styles=True):
        console.print(Text.from_markup(f"[yellow]? {escape(panel.current_question_text)}[/yellow]"))
        console.print()
        for r in panel.render_full_body():
            console.print(r)
