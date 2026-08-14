# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Responsive layout, composer, panes, and visual preference controller."""

from __future__ import annotations

from typing import Optional

from textual import events, on
from textual.css.query import NoMatches
from textual.widgets import Static, TextArea

from coderAI.tui.rendering import (
    composer_footer_markup,
    composer_token_counter,
    render_composer_context,
)
from coderAI.tui.screens import PromptArea
from coderAI.tui.theme import ThemeMode, Tokens

PANE_LEFT_MIN_COLS = 100
PANE_RIGHT_MIN_COLS = 130
MIN_PANE_WIDTH = 20
MAX_PANE_WIDTH = 60
DEFAULT_LEFT_WIDTH = 32
DEFAULT_RIGHT_WIDTH = 35


class AppLayoutController:
    def on_resize(self, event: events.Resize) -> None:
        # self.size still reports the pre-resize value while this handler
        # runs; the event carries the new terminal size.
        self._apply_responsive_layout(event.size.width)
        self._update_token_counter()
        self._update_composer_footer_width(event.size.width)

    def _update_composer_footer_width(self, width: int | None = None) -> None:
        try:
            w = width if width is not None else self.size.width
            footer = self.query_one("#composer-footer", Static)
            footer.update(composer_footer_markup(self.reducer.session, width=w))
        except Exception:
            pass

    def _update_token_counter(self) -> None:
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            counter = self.query_one("#composer-token-counter", Static)
            text = prompt.text or ""
            # Show counter only when there is content or ctx limit known
            if text.strip():
                counter.update(composer_token_counter(text, self.reducer.session.ctx_limit))
                counter.display = True
            else:
                counter.update("")
                counter.display = False
        except Exception:
            pass

    def _update_autocomplete(self, text: str, cursor_col: int) -> None:
        """Inline @ autocomplete: show filtered file list as user types @prefix."""
        try:
            auto = self.query_one("#autocomplete", Static)
        except NoMatches:
            return
        # Find @ word before cursor
        row, col = 0, cursor_col
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            row, col = prompt.cursor_location
            line = prompt.document.get_line(row)
            text_before = line[:col]
        except Exception:
            text_before = text
        at_idx = text_before.rfind("@")
        if at_idx == -1:
            auto.display = False
            auto.remove_class("has-items")
            return
        # Check word boundary before @
        if at_idx > 0 and not text_before[at_idx - 1].isspace():
            auto.display = False
            auto.remove_class("has-items")
            return
        prefix = text_before[at_idx + 1 :].lower()
        # Allow empty prefix to show top 5, otherwise filter
        if " " in prefix or "\n" in prefix:
            auto.display = False
            auto.remove_class("has-items")
            return
        files = self.project_files or []
        filtered = [f for f in files if prefix in f.lower()] if prefix else files[:5]
        if not filtered:
            auto.display = False
            auto.remove_class("has-items")
            return
        # Show up to 6 suggestions
        show = filtered[:6]
        lines = []
        for f in show:
            # Highlight prefix
            if prefix:
                # simple highlight
                low = f.lower()
                idx = low.find(prefix)
                if idx != -1:
                    before = f[:idx]
                    match = f[idx : idx + len(prefix)]
                    after = f[idx + len(prefix) :]
                    lines.append(
                        f"  [{Tokens.TEXT}]{before}[/][bold {Tokens.ACCENT}]{match}[/][{Tokens.TEXT}]{after}[/]"
                    )
                else:
                    lines.append(f"  {f}")
            else:
                lines.append(f"  {f}")
        lines.append(f"[{Tokens.TEXT_MUTED}]  ↵ to accept first · Esc to dismiss · @ to pin[/]")
        auto.update("\n".join(lines))
        auto.display = True
        auto.add_class("has-items")

    @on(events.Paste)
    def _on_paste(self, event: events.Paste) -> None:
        """Paste-image → vision tool wiring: detect image data in paste."""
        text = event.text or ""
        # Heuristic: base64 image or file path ending with image ext
        image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        if text.strip().lower().endswith(image_exts):
            # Treat as image path paste
            if self.controller:
                self.controller.enqueue_command("vision", path=text.strip(), source="paste")
                self.notify(f"Pasted image {text.strip()} → vision")
            return
        # Check for base64 image data URL
        if text.startswith("data:image/") or (len(text) > 100 and text.startswith("iVBOR")):
            if self.controller:
                self.controller.enqueue_command("vision", data=text[:4000], source="paste")
                self.notify("Pasted image data → vision")
            return

    def _auto_pane_visibility(self, width: Optional[int] = None) -> tuple[bool, bool]:
        """(left, right) pane visibility earned by the terminal width."""
        w = self.size.width if width is None else width
        return w >= PANE_LEFT_MIN_COLS, w >= PANE_RIGHT_MIN_COLS

    def _apply_responsive_layout(self, width: Optional[int] = None) -> None:
        w = self.size.width if width is None else width
        if w <= 0:
            return
        auto_left, auto_right = self._auto_pane_visibility(w)
        for selector, pref, auto in (
            ("#left-pane", self._left_pane_pref, auto_left),
            ("#right-pane", self._right_pane_pref, auto_right),
        ):
            try:
                pane = self.query_one(selector)
            except NoMatches:
                continue
            pane.display = auto if pref is None else pref

    def _apply_pane_widths(self) -> None:
        """Apply stored _left_width / _right_width to the DOM."""
        try:
            self.query_one("#left-pane").styles.width = str(self._left_width)
        except Exception:
            pass
        try:
            self.query_one("#right-pane").styles.width = str(self._right_width)
        except Exception:
            pass

    def _refresh_composer_context(self) -> None:
        """Update the composer context chips + ctx gauge."""
        try:
            s = self.reducer.session
            ctx = self.query_one("#composer-context", Static)
            ctx.update(render_composer_context(s))
            has_any = bool((s.context_files or []) or s.ctx_limit)
            if has_any:
                ctx.add_class("has-context")
            else:
                # Keep hint visible even when no limit yet — remove class only if truly empty
                # The helper already shows a hint, so always show the bar.
                ctx.add_class("has-context")
        except Exception:
            pass

    def _resize_pane(self, which: str, delta: int) -> None:
        if which == "left":
            self._left_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, self._left_width + delta))
            try:
                self.query_one("#left-pane").styles.width = str(self._left_width)
            except Exception:
                pass
            self.notify(f"Agents pane {self._left_width} cols")
        else:
            self._right_width = max(MIN_PANE_WIDTH, min(MAX_PANE_WIDTH, self._right_width + delta))
            try:
                self.query_one("#right-pane").styles.width = str(self._right_width)
            except Exception:
                pass
            self.notify(f"Tasks pane {self._right_width} cols")

    def action_increase_left_pane(self) -> None:
        self._resize_pane("left", 2)

    def action_decrease_left_pane(self) -> None:
        self._resize_pane("left", -2)

    def action_increase_right_pane(self) -> None:
        self._resize_pane("right", 2)

    def action_decrease_right_pane(self) -> None:
        self._resize_pane("right", -2)

    def action_reset_pane_widths(self) -> None:
        self._left_width = DEFAULT_LEFT_WIDTH
        self._right_width = DEFAULT_RIGHT_WIDTH
        self._apply_pane_widths()
        self.notify(f"Panes reset {self._left_width}/{self._right_width}")

    def action_toggle_theme(self) -> None:
        self._theme_mode = (
            ThemeMode.HIGH_CONTRAST if self._theme_mode == ThemeMode.DARK else ThemeMode.DARK
        )
        # Re-apply scrollbar colors via stylesheet update is not dynamic for existing widgets,
        # so poke styles directly for next refresh. CSS vars would be cleaner but this avoids full relayout.
        col = ThemeMode.scrollbar(self._theme_mode)
        for sel in ("#timeline", "#agent-tree-scroll", "#tasks-scroll"):
            try:
                self.query_one(sel).styles.scrollbar_color = col  # type: ignore[attr-defined]
            except Exception:
                pass
        self.notify(f"Theme: {self._theme_mode}")
        self._refresh_ui("chrome")

    def action_toggle_notifications(self) -> None:
        self._notifications_muted = not self._notifications_muted
        self.notify(f"Notifications {'muted' if self._notifications_muted else 'on'}")

    def action_clear_toasts(self) -> None:
        # Remove toast items from timeline (keep non-toast)
        before = len(self.reducer.timeline)
        self.reducer.timeline = [it for it in self.reducer.timeline if it.get("kind") != "toast"]
        removed = before - len(self.reducer.timeline)
        if removed:
            self._refresh_ui("full")
            self.notify(f"Cleared {removed} toasts")
        else:
            self.notify("No toasts to clear")

    def action_mute_toast_level(self, level: str) -> None:
        if level in self._muted_toast_levels:
            self._muted_toast_levels.remove(level)
            self.notify(f"Unmuted {level} toasts")
        else:
            self._muted_toast_levels.add(level)
            self.notify(f"Muted {level} toasts")

    def _toggle_pane(self, selector: str, auto: bool, label: str) -> Optional[bool]:
        """Flip a pane and return the new user override (None = back to auto)."""
        try:
            pane = self.query_one(selector)
        except NoMatches:
            return None
        show = not pane.display
        pane.display = show
        self.notify(f"{label} pane {'shown' if show else 'hidden'}")
        # An override that matches the auto state is no override at all —
        # drop it so future resizes keep auto-hiding/showing as expected.
        return None if show == auto else show

    def action_toggle_left_pane(self) -> None:
        auto_left, _ = self._auto_pane_visibility()
        self._left_pane_pref = self._toggle_pane("#left-pane", auto_left, "Agents")

    def action_toggle_right_pane(self) -> None:
        _, auto_right = self._auto_pane_visibility()
        self._right_pane_pref = self._toggle_pane("#right-pane", auto_right, "Tasks")

    @on(TextArea.Changed, "#prompt-area")
    def _on_prompt_changed(self, event: TextArea.Changed) -> None:
        text = (
            event.text_area.text
            if hasattr(event, "text_area")
            else event.value
            if hasattr(event, "value")
            else ""
        )
        # Fallback to prompt text
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            text = prompt.text
        except Exception:
            pass
        self._update_token_counter()
        try:
            prompt = self.query_one("#prompt-area", PromptArea)
            _, col = prompt.cursor_location
            self._update_autocomplete(text, col)
        except Exception:
            self._update_autocomplete(text, len(text))
