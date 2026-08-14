# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Keyboard, palette, navigation, expansion, and submission controller."""

from __future__ import annotations

import time
from typing import Any, Optional
from collections.abc import Callable

from textual import events, on
from textual.css.query import NoMatches
from textual.widgets import Static

from coderAI.tui.screens import (
    CommandPaletteScreen,
    FilePickerScreen,
    FullContentScreen,
    PromptArea,
    SearchScreen,
)
from coderAI.tui.slash import handle_slash_command
from coderAI.tui.theme import Tokens
from coderAI.tui.timeline_render import calculate_item_lines
from coderAI.tui.widgets import SelectableRichLog


class AppInputController:
    @on(events.Key)
    def _on_app_key(self, event: events.Key) -> None:
        # Handle Tab to accept autocomplete first suggestion
        if event.key == "tab":
            try:
                auto = self.query_one("#autocomplete", Static)
                if auto.display:
                    # Accept first filtered file
                    prompt = self.query_one("#prompt-area", PromptArea)
                    row, col = prompt.cursor_location
                    line = prompt.document.get_line(row)
                    at_idx = line.rfind("@", 0, col)
                    if at_idx != -1:
                        prefix = line[at_idx + 1 : col]
                        files = self.project_files or []
                        filtered = (
                            [f for f in files if prefix.lower() in f.lower()]
                            if prefix
                            else files[:5]
                        )
                        if filtered:
                            chosen = filtered[0]
                            # Replace @prefix with @chosen + space
                            new_line = line[: at_idx + 1] + chosen + " " + line[col:]
                            doc = prompt.document
                            start_idx = doc.get_index_from_location((row, 0))  # type: ignore[attr-defined]
                            end_idx = doc.get_index_from_location((row, col))  # type: ignore[attr-defined]
                            prompt.text = doc.text[:start_idx] + new_line + doc.text[end_idx:]
                            # Simpler: replace via insert
                            prompt.text = (
                                prompt.text[:at_idx] + f"@{chosen} " + prompt.text[col:]
                                if False
                                else prompt.text
                            )
                            # Fallback: just insert
                            prompt.insert(f"{chosen} ")
                            event.stop()
                            event.prevent_default()
                            auto.display = False
                            auto.remove_class("has-items")
                            self._update_token_counter()
                            return
            except Exception:
                pass

    def action_cancel_turn(self) -> None:
        if len(self.screen_stack) > 1:
            return
        if self.controller:
            self.controller.enqueue_command("cancel")

    def action_ctrl_c(self) -> None:
        # _confirm_exit arms on first press (returns False) and exits on a
        # second press within the window (returns True).
        if not self._confirm_exit():
            self.notify("Press Ctrl+C again within 5s to exit")

    def _timeline_log(self) -> Optional[SelectableRichLog]:
        try:
            return self.query_one("#timeline", SelectableRichLog)
        except NoMatches:
            return None

    def _scroll_timeline(self, scroll: Callable[[SelectableRichLog], None]) -> None:
        log = self._timeline_log()
        if log is not None:
            scroll(log)

    def action_timeline_page_up(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_page_up())

    def action_timeline_page_down(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_page_down())

    def action_timeline_scroll_top(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_home(animate=False))

    def action_timeline_scroll_bottom(self) -> None:
        self._scroll_timeline(lambda log: log.scroll_end(animate=False))

    def action_copy_selection(self) -> None:
        from coderAI.tui.clipboard import copy_text

        text = self.screen.get_selected_text()
        if not text:
            self.notify("Nothing selected", severity="warning")
            return
        copy_text(
            text,
            write_osc52=self._osc52_writer(),
            notify_fn=self.notify,
        )

    def _osc52_writer(self):
        """Return a writer that sends OSC-52 through Textual's terminal driver."""
        driver = self._driver

        def write(sequence: str) -> None:
            if driver is not None:
                driver.write(sequence)
            else:
                import sys

                sys.stdout.write(sequence)
                sys.stdout.flush()

        return write

    def _find_last_content_item(self) -> Optional[tuple[int, dict[str, Any]]]:
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            if it.get("kind") in ("user", "assistant", "tool", "diff"):
                return i, it
        return None

    def _find_visible_content_item(self) -> Optional[tuple[int, dict[str, Any]]]:
        try:
            log = self.query_one("#timeline", SelectableRichLog)
        except NoMatches:
            return self._find_last_content_item()
        scroll_y = log.scroll_offset.y if log.scroll_offset else 0
        if scroll_y == 0:
            return self._find_last_content_item()

        width = log.scrollable_content_region.width
        verbose = self.reducer.session.verbose
        estimated_lines = 0
        for i in range(len(self.reducer.timeline) - 1, -1, -1):
            it = self.reducer.timeline[i]
            # Accumulate every item's height so the running offset matches what
            # is actually rendered; only content items can be the return target.
            if it.get("streaming", False):
                lines = calculate_item_lines(it, verbose, width)
            else:
                key = (it.get("id"), verbose, bool(it.get("collapsed")), width)
                cached = self._line_count_cache.get(key)
                if cached is None:
                    cached = calculate_item_lines(it, verbose, width)
                    self._line_count_cache[key] = cached
                lines = cached
            estimated_lines += lines
            if it.get("kind") not in ("user", "assistant", "tool", "diff"):
                continue
            if estimated_lines >= scroll_y:
                return i, it
        return self._find_last_content_item()

    def action_toggle_collapse(self) -> None:
        found = self._find_visible_content_item()
        if found is None:
            self.notify("No item to collapse")
            return
        idx, it = found
        it["collapsed"] = not it.get("collapsed", False)
        state = "collapsed" if it["collapsed"] else "expanded"
        self.notify(f"{it.get('kind', 'item').capitalize()} {state}")
        self._refresh_ui("full")

    def action_expand_full(self) -> None:
        found = self._find_visible_content_item()
        if found is None:
            self.notify("No item to expand")
            return
        _, it = found
        kind = it.get("kind", "")
        if kind == "diff":
            # Prefer inline hunk review with toggles + find-in-diff
            path = str(it.get("path", "") or "diff")
            diff_text = str(it.get("diff", "") or "")
            # Use DiffReviewScreen for interactive toggles
            self.run_worker(self._show_diff_review(path, diff_text), exclusive=True)
            return
        elif kind in ("assistant",):
            title = "Full Assistant Response"
            content = str(it.get("content", ""))
        elif kind == "user":
            title = "Full User Message"
            content = str(it.get("text", ""))
        elif kind == "tool":
            name = str(it.get("name") or "tool")
            title = f"Tool — {name}"
            # Detect syntax for highlighted preview (file extension or known lang)
            preview = str(it.get("preview") or "")
            args = it.get("args") or {}
            syntax = None
            # Guess syntax from preview content or args path
            candidate = ""
            if isinstance(args, dict):
                candidate = str(args.get("path") or args.get("file_path") or "")
            if candidate:
                ext = candidate.rsplit(".", 1)[-1].lower() if "." in candidate else ""
                syntax_map = {
                    "py": "python",
                    "js": "javascript",
                    "ts": "typescript",
                    "json": "json",
                    "md": "markdown",
                    "sh": "bash",
                    "yaml": "yaml",
                    "yml": "yaml",
                    "toml": "toml",
                    "rs": "rust",
                    "go": "go",
                }
                syntax = syntax_map.get(ext)
            if not syntax and preview:
                # Heuristic: look for python keywords
                if "def " in preview or "import " in preview:
                    syntax = "python"
                elif "{" in preview and ":" in preview:
                    syntax = "json"
            parts: list[str] = [f"Tool: {name}"]
            cat = str(it.get("category") or "")
            risk = str(it.get("risk") or "")
            if cat:
                parts.append(f"Category: {cat}")
            if risk and risk != "low":
                parts.append(f"Risk: {risk}")
            if args:
                import json as _json

                try:
                    args_str = (
                        _json.dumps(args, indent=2, ensure_ascii=False)
                        if isinstance(args, dict)
                        else str(args)
                    )
                except Exception:
                    args_str = str(args)
                parts.append(f"\nArgs:\n{args_str}")
            if preview:
                parts.append(f"\nPreview:\n{preview}")
            err = str(it.get("error") or "")
            if err:
                parts.append(f"\nError:\n{err}")
            ok = it.get("ok")
            status = "running" if ok is None else ("ok" if ok else "error")
            parts.append(f"\nStatus: {status}")
            if it.get("full_available"):
                parts.append("(full output truncated in timeline — shown here)")
            content = "\n".join(parts)
            # Pass detected syntax to FullContentScreen for highlighted preview
            self._expand_syntax = syntax  # type: ignore[attr-defined]
        elif kind == "error":
            title = "Error"
            content = str(it.get("message", ""))
            hint = str(it.get("hint") or "")
            details = str(it.get("details") or "")
            if hint:
                content += f"\n\nHint: {hint}"
            if details:
                content += f"\n\nDetails:\n{details}"
        elif kind == "toast":
            title = f"Toast — {it.get('level', 'info')}"
            content = str(it.get("message", ""))
        elif kind in ("skill_card", "plan_card", "approval"):
            title = kind.replace("_", " ").title()
            import json as _json

            try:
                content = _json.dumps(it, indent=2, ensure_ascii=False, default=str)
            except Exception:
                content = str(it)
        else:
            self.notify(f"Cannot expand {kind} items")
            return
        if not content.strip():
            self.notify("No content to show")
            return
        syntax = getattr(self, "_expand_syntax", None)
        # Clean temp attr
        if hasattr(self, "_expand_syntax"):
            try:
                delattr(self, "_expand_syntax")
            except Exception:
                pass
        self.run_worker(self._show_full_content(title, content, syntax), exclusive=True)

    async def _show_full_content(self, title: str, content: str, syntax: str | None = None) -> None:
        await self.push_screen_wait(FullContentScreen(title, content, syntax=syntax))

    async def _show_diff_review(self, path: str, diff: str) -> None:
        from coderAI.tui.screens import DiffReviewScreen

        result = await self.push_screen_wait(DiffReviewScreen(path, diff))
        if result and self.controller:
            # Apply per-hunk toggles: send as tool command if needed
            toggles = result.get("toggles", {})
            # For now just notify and emit as info toast; backend will handle actual patch apply
            accepted = sum(1 for v in toggles.values() if v is True)
            rejected = sum(1 for v in toggles.values() if v is False)
            self.notify(f"Diff review {path}: {accepted} accepted, {rejected} rejected")
            # Optionally send to controller as diff decision
            try:
                self.controller.enqueue_command("apply_diff_toggles", path=path, toggles=toggles)
            except Exception:
                pass

    def action_file_mention(self) -> None:
        self.run_worker(self._show_file_mention(), exclusive=True)

    async def _show_file_mention(self) -> None:
        if self.project_files:
            # Serve the cached scan immediately; refresh it in the background
            # for the next invocation (_scan_in_progress guards concurrency).
            self.run_worker(self._scan_project_files)  # type: ignore[arg-type]
        else:
            await self._scan_project_files()
        error = getattr(self, "_scan_error", None)
        result = await self.push_screen_wait(
            FilePickerScreen(
                self.project_files,
                placeholder="🔍 Type to search files to mention and pin…",
                footer_help=(
                    f"[{Tokens.TEXT_MUTED}]↑↓ navigate  ↵ mention + pin  ⎋ close · g gitignore[/]"
                ),
                error=error,
            )
        )
        prompt = self.query_one("#prompt-area", PromptArea)
        if result:
            prompt.insert(f"@{result} ")
            if self.controller:
                self.controller.enqueue_command("manage_context", action="add", path=result)
        prompt.focus()

    def action_command_palette(self) -> None:
        self.run_worker(self._show_palette(), exclusive=True)

    async def _show_palette(self, only_section: Optional[str] = None) -> None:
        result = await self.push_screen_wait(
            CommandPaletteScreen(self.reducer.session, only_section)
        )
        if result is None or not self.controller:
            return
        r = result.strip()
        if r.startswith("/"):
            if r in ("/help", "/?"):
                return
            self._submit(r)
        else:
            self.query_one("#prompt-area", PromptArea).text = r
            self.query_one("#prompt-area", PromptArea).focus()

    @on(PromptArea.Submitted)
    def _on_prompt_submitted(self, event: PromptArea.Submitted) -> None:
        if not self.reducer.session.ready:
            return
        prompt = self.query_one("#prompt-area", PromptArea)
        prompt.text = ""
        text = event.text.strip()
        if not text:
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        if not self.controller:
            return
        if text.startswith("/"):
            handled = handle_slash_command(
                text,
                self.controller,
                self.reducer,
                show_palette=self._show_palette_section,
                show_search=self._show_search,
                show_context=self._show_context,
                clear_context=self._clear_context,
                toggle_verbose=self._toggle_verbose,
                reveal_reasoning=self._reveal_reasoning,
                confirm_exit=self._confirm_exit,
                set_search_filter=lambda q: setattr(self, "_search_filter", q),
                retry_agent=self._retry_agent,
                rewind_timeline=self._rewind_timeline,
                resume_session=self._resume_session,
                copy_to_clipboard=self._osc52_writer(),
            )
            if handled:
                return
        self.reducer._push({"kind": "user", "id": self.reducer.next_id(), "text": text})
        self.reducer._bump_refresh("append")
        self.reducer._notify()
        self._awaiting_response = True
        self.controller.enqueue_command("send_message", text=text)

    def _show_palette_section(self, section: str | None = None) -> None:
        self.run_worker(self._show_palette(section), exclusive=True)

    def _show_search(self) -> None:
        # push_screen is synchronous; no worker/async wrapper needed.
        self.push_screen(SearchScreen(self.reducer.timeline, self._search_filter))

    def _show_context(self) -> None:
        files = self.reducer.session.context_files or []
        msg = "\n".join(f"  {f.get('path')} ({f.get('size', 0)} B)" for f in files) or "(none)"
        self._toast("info", f"Pinned context:\n{msg}")

    def _reset_timeline_view(self) -> None:
        """Repaint after clearing/truncating the timeline and re-pin to bottom."""
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._refresh_ui("full")
        try:
            self.query_one("#timeline", SelectableRichLog).scroll_end(animate=False)
        except NoMatches:
            pass

    def _clear_context(self) -> None:
        if self.controller:
            self.controller.enqueue_command("clear_context")
        self.reducer.timeline.clear()
        self._reset_timeline_view()

    def _rewind_timeline(self, turn: int) -> None:
        """Truncate the local timeline to before the Nth user message.

        Mirrors ``_clear_context`` but stops at a turn boundary instead of
        wiping everything; the backend ``rewind`` command truncates the
        session history in parallel.
        """
        count = 0
        cut_idx: Optional[int] = None
        for i, it in enumerate(self.reducer.timeline):
            if it.get("kind") == "user":
                count += 1
                if count == turn:
                    cut_idx = i
                    break
        if cut_idx is None:
            return
        del self.reducer.timeline[cut_idx:]
        self._line_count_cache.clear()
        self._log_rendered_idx = 0
        self._refresh_ui("full")
        try:
            self.query_one("#timeline", SelectableRichLog).scroll_end(animate=False)
        except NoMatches:
            pass

    def _toggle_verbose(self) -> None:
        self.reducer.session.verbose = not self.reducer.session.verbose
        level = "verbose" if self.reducer.session.verbose else "normal"
        if self.controller:
            self.controller.enqueue_command("set_verbosity", level=level)
        self.notify(f"Verbose {'on' if self.reducer.session.verbose else 'off'}")

    def _reveal_reasoning(self) -> None:
        for it in reversed(self.reducer.timeline):
            if it.get("kind") == "assistant" and (it.get("reasoning") or "").strip():
                self._toast("info", it["reasoning"][:4000])
                return
        self.notify("No reasoning to reveal")

    def _confirm_exit(self) -> bool:
        now = time.monotonic()
        if self._exit_armed_at and now - self._exit_armed_at < 5:
            if self.controller:
                self.controller.enqueue_command("exit")
            self.exit()
            return True
        self._exit_armed_at = now
        # Show visual countdown bar (5s) instead of just toast
        try:
            bar = self.query_one("#countdown-bar", Static)
            bar.display = True
            bar.add_class("active")
            bar.styles.width = "100%"
            # Animate width down over 5s via interval
            if hasattr(self, "_countdown_timer") and self._countdown_timer:
                self._countdown_timer.stop()
            start = now

            def _tick() -> None:
                elapsed = time.monotonic() - start
                pct = max(0, 100 - int(elapsed / 5 * 100))
                try:
                    b = self.query_one("#countdown-bar", Static)
                    b.styles.width = f"{pct}%"
                    if pct <= 0:
                        b.display = False
                        b.remove_class("active")
                        if hasattr(self, "_countdown_timer"):
                            self._countdown_timer.stop()
                except Exception:
                    pass

            self._countdown_timer = self.set_interval(0.1, _tick)
            self.notify("Press Ctrl+C again within 5s to exit")
        except Exception:
            self.notify("Press Ctrl+C again within 5s to exit")
        return False

    def action_increase_font(self) -> None:
        """Font-size control (a11y)."""
        try:
            cur = getattr(self, "_font_size", 1.0)
            self._font_size = min(1.5, cur + 0.1)
            self.notify(f"Font size {self._font_size:.1f}x")
        except Exception:
            pass

    def action_decrease_font(self) -> None:
        try:
            cur = getattr(self, "_font_size", 1.0)
            self._font_size = max(0.8, cur - 0.1)
            self.notify(f"Font size {self._font_size:.1f}x")
        except Exception:
            pass

    def action_reset_font(self) -> None:
        self._font_size = 1.0  # type: ignore[attr-defined]
        self.notify("Font size reset")

    def action_show_config(self) -> None:
        self.run_worker(self._show_config_screen(), exclusive=True)

    async def _show_config_screen(self) -> None:
        from coderAI.tui.screens import ConfigScreen

        cur = {
            "model": self.reducer.session.model or "",
            "budget": str(self.reducer.session.budget_usd or ""),
            "notifications": "on" if not getattr(self, "_notifications_muted", False) else "off",
        }
        result = await self.push_screen_wait(ConfigScreen(cur))
        if not result:
            return
        # Apply changes
        if result.get("model"):
            self.reducer.session.model = result["model"]
            if self.controller:
                self.controller.enqueue_command("set_model", model=result["model"])
        if result.get("budget"):
            try:
                self.reducer.session.budget_usd = float(result["budget"])
            except Exception:
                pass
        notif = result.get("notifications", "").lower()
        if notif in ("off", "false", "0"):
            self._notifications_muted = True
        elif notif in ("on", "true", "1"):
            self._notifications_muted = False
        self.notify("Configuration updated")
        self._refresh_ui("chrome")
