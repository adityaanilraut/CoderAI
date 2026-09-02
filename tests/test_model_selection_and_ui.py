"""Tests for model selection, UI menus, and autocompletion."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from coderai.cli.interactive_menu import (
    CURATED_MODELS,
    select_model_interactive,
    select_with_arrows,
)
from coderai.cli.prompt_session import (
    HAS_PTK,
    CoderAIPromptSession,
    SlashCommandCompleter,
    get_bottom_toolbar_tokens,
)
from coderai.cli.statusline import get_git_branch, get_git_status


class TestModelSelectionAndUI(unittest.TestCase):
    def test_select_model_interactive_by_index(self) -> None:
        with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=0):
            chosen = select_model_interactive(None, "gpt-5.6-luna")
            self.assertEqual(chosen, CURATED_MODELS[0][0])

    def test_select_model_interactive_by_fuzzy_name(self) -> None:
        with patch("coderai.cli.interactive_menu.select_with_arrows", return_value="claude"):
            chosen = select_model_interactive(None, "gpt-5.6-luna")
            self.assertEqual(chosen, "claude-3-7-sonnet")

        with patch("coderai.cli.interactive_menu.select_with_arrows", return_value="gemini"):
            chosen = select_model_interactive(None, "gpt-5.6-luna")
            self.assertEqual(chosen, "gemini-3.7-flash")

        with patch("coderai.cli.interactive_menu.select_with_arrows", return_value="deepseek"):
            chosen = select_model_interactive(None, "gpt-5.6-luna")
            self.assertEqual(chosen, "deepseek-v4-pro")

    def test_select_model_interactive_custom_model(self) -> None:
        with patch(
            "coderai.cli.interactive_menu.select_with_arrows",
            return_value="my-custom-llm:latest",
        ):
            chosen = select_model_interactive(None, "gpt-5.6-luna")
            self.assertEqual(chosen, "my-custom-llm:latest")

    def test_select_model_interactive_cancel_returns_current(self) -> None:
        with patch("coderai.cli.interactive_menu.select_with_arrows", return_value=None):
            chosen = select_model_interactive(None, "gpt-5.6-terra")
            self.assertEqual(chosen, "gpt-5.6-terra")

    def test_select_with_arrows_non_tty_index(self) -> None:
        items = [("m1", "Model 1", "Desc 1"), ("m2", "Model 2", "Desc 2")]
        with patch("sys.stdin.isatty", return_value=False):
            with patch("builtins.input", return_value="2"):
                res = select_with_arrows(None, items, default_idx=0)
                self.assertEqual(res, 1)

    def test_select_with_arrows_non_tty_custom(self) -> None:
        items = [("m1", "Model 1", "Desc 1"), ("m2", "Model 2", "Desc 2")]
        with patch("sys.stdin.isatty", return_value=False):
            # First input selects custom slot (3), second input enters custom text
            with patch("builtins.input", side_effect=["3", "custom-model-id"]):
                res = select_with_arrows(None, items, default_idx=0, allow_custom=True)
                self.assertEqual(res, "custom-model-id")

    def test_select_with_arrows_non_tty_cancel(self) -> None:
        items = [("m1", "Model 1", "Desc 1")]
        with patch("sys.stdin.isatty", return_value=False):
            with patch("builtins.input", return_value="q"):
                res = select_with_arrows(None, items, default_idx=0, allow_cancel=True)
                self.assertIsNone(res)

    def test_bottom_toolbar_tokens_contains_model(self) -> None:
        tokens = get_bottom_toolbar_tokens(".", plan_mode=False, active_model="gpt-5.6-sol")
        rendered_texts = [t[1] for t in tokens]
        self.assertTrue(any("gpt-5.6-sol" in text for text in rendered_texts))

    def test_bottom_toolbar_tokens_plan_mode(self) -> None:
        tokens = get_bottom_toolbar_tokens(".", plan_mode=True, active_model="gemini-3.7-flash")
        rendered_texts = [t[1] for t in tokens]
        self.assertTrue(any("plan" in text for text in rendered_texts))
        self.assertTrue(any("gemini-3.7-flash" in text for text in rendered_texts))

    def test_git_status_and_branch_helpers(self) -> None:
        branch, dirty = get_git_status(".")
        b_str = get_git_branch(".")
        if branch:
            self.assertIn(branch, b_str if b_str else "")

    @unittest.skipUnless(HAS_PTK, "prompt_toolkit required")
    def test_slash_completer_subarguments(self) -> None:
        from prompt_toolkit.document import Document

        commands = [
            MagicMock(
                name="model",
                summary="Model switcher",
                description="Model switcher",
                aliases=[],
                subcommands=(),
            ),
            MagicMock(
                name="plan",
                summary="Plan mode",
                description="Plan mode",
                aliases=[],
                subcommands=("on", "off"),
            ),
        ]
        # set attributes explicitly on mocks
        commands[0].name = "model"
        commands[0].summary = "Model switcher"
        commands[0].description = "Model switcher"
        commands[0].aliases = []
        commands[0].subcommands = ()

        commands[1].name = "plan"
        commands[1].summary = "Plan mode"
        commands[1].description = "Plan mode"
        commands[1].aliases = []
        commands[1].subcommands = ("on", "off")

        completer = SlashCommandCompleter(commands, project_root=".")

        # 1. Complete slash command
        doc = Document(text="/mod", cursor_position=4)
        completions = list(completer.get_completions(doc, None))
        comp_texts = [c.text for c in completions]
        self.assertTrue(any("/model" in t for t in comp_texts))

        # 2. Complete /model sub-argument
        doc_model = Document(text="/model gem", cursor_position=10)
        completions_model = list(completer.get_completions(doc_model, None))
        comp_model_texts = [c.text for c in completions_model]
        self.assertIn("gemini-3.7-flash", comp_model_texts)

        # 3. Complete /plan sub-argument
        doc_plan = Document(text="/plan o", cursor_position=7)
        completions_plan = list(completer.get_completions(doc_plan, None))
        comp_plan_texts = [c.text for c in completions_plan]
        self.assertIn("on", comp_plan_texts)
        self.assertIn("off", comp_plan_texts)

    def test_read_single_key_escape_sequences(self) -> None:
        from coderai.cli.interactive_menu import _read_single_key

        test_sequences = [
            ("\x1b[A", "UP"),
            ("\x1bOA", "UP"),
            ("\x1b[B", "DOWN"),
            ("\x1bOB", "DOWN"),
            ("\x1b[C", "RIGHT"),
            ("\x1b[D", "LEFT"),
            ("\x1b[H", "HOME"),
            ("\x1b[F", "END"),
            ("\x1b[5~", "PAGE_UP"),
            ("\x1b[6~", "PAGE_DOWN"),
            ("\x1b[3~", "DELETE"),
            ("\x1b", "ESCAPE"),
            ("\r", "ENTER"),
            ("\n", "ENTER"),
            ("\x03", "CTRL_C"),
            ("\x7f", "BACKSPACE"),
            ("\t", "TAB"),
            ("a", "a"),
        ]

        for seq_str, expected in test_sequences:
            mock_stdin = MagicMock()
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            chars = iter(seq_str)
            mock_stdin.read.side_effect = lambda _, it=chars: next(it, "")

            with patch("sys.stdin", mock_stdin):
                with patch(
                    "select.select",
                    return_value=([mock_stdin], [], []) if len(seq_str) > 1 else ([], [], []),
                ):
                    with patch("termios.tcgetattr", return_value=[]):
                        with patch("termios.tcsetattr", return_value=None):
                            with patch("tty.setraw", return_value=None):
                                res = _read_single_key()
                                self.assertEqual(res, expected, f"Failed for sequence {seq_str!r}")

    def test_select_with_arrows_interactive_up_down_navigation(self) -> None:
        items = [
            ("model-a", "Model A", "First"),
            ("model-b", "Model B", "Second"),
            ("model-c", "Model C", "Third"),
        ]
        # Start at index 0, press UP (wraps to index 2), then press ENTER -> returns 2
        with patch("sys.stdin.isatty", return_value=True):
            with patch(
                "coderai.cli.interactive_menu._read_single_key", side_effect=["UP", "ENTER"]
            ):
                res = select_with_arrows(None, items, default_idx=0)
                self.assertEqual(res, 2)

        # Start at index 1, press UP -> moves to index 0, then press ENTER -> returns 0
        with patch("sys.stdin.isatty", return_value=True):
            with patch(
                "coderai.cli.interactive_menu._read_single_key", side_effect=["UP", "ENTER"]
            ):
                res = select_with_arrows(None, items, default_idx=1)
                self.assertEqual(res, 0)

        # Start at index 1, press DOWN -> moves to index 2, then press ENTER -> returns 2
        with patch("sys.stdin.isatty", return_value=True):
            with patch(
                "coderai.cli.interactive_menu._read_single_key", side_effect=["DOWN", "ENTER"]
            ):
                res = select_with_arrows(None, items, default_idx=1)
                self.assertEqual(res, 2)

    def test_bottom_toolbar_tokens_with_full_session_stats(self) -> None:
        tokens = get_bottom_toolbar_tokens(
            ".",
            plan_mode=True,
            active_model="gpt-5.6-luna",
            tokens=6632,
            turns=4,
            mcp_count=2,
        )
        rendered_texts = [t[1] for t in tokens]
        self.assertTrue(any("gpt-5.6-luna" in text for text in rendered_texts))
        self.assertTrue(any("6.6k" in text or "6,632" in text for text in rendered_texts))
        self.assertTrue(any("plan: ON" in text for text in rendered_texts))
        self.assertTrue(any("turns: 4" in text for text in rendered_texts))
        self.assertTrue(any("mcp: 2" in text for text in rendered_texts))

    @unittest.skipUnless(HAS_PTK, "prompt_toolkit required")
    def test_prompt_session_updates_session_stats_and_styles(self) -> None:
        session = CoderAIPromptSession(".", lambda: "gpt-5.6-luna", plan_mode=False)
        session.update_session_stats(tokens=5000, turns=3, mcp_count=1, plan_mode=True)
        self.assertEqual(session._tokens, 5000)
        self.assertEqual(session._turns, 3)
        self.assertEqual(session._mcp_count, 1)
        self.assertTrue(session.plan_mode)

        # Verify completion styling removes fuzzymatch character highlights
        style_rules = dict(session._style.style_rules)
        self.assertIn("fuzzymatch.inside", style_rules)
        self.assertIn("nobold", style_rules["fuzzymatch.inside"])
        self.assertIn("nounderline", style_rules["fuzzymatch.inside"])

    def test_interactive_menu_panel_rendering_without_background_highlights(self) -> None:
        from coderai.cli.interactive_menu import select_with_arrows

        items = [("opt1", "Option 1", "Desc 1"), ("opt2", "Option 2", "Desc 2")]
        # Mock rendering panel
        with patch("coderai.cli.interactive_menu.Panel") as mock_panel:
            with patch("sys.stdin.isatty", return_value=True):
                with patch("coderai.cli.interactive_menu._read_single_key", side_effect=["ENTER"]):
                    select_with_arrows(None, items, default_idx=0)
                    # Check panel content if called
                    if mock_panel.called:
                        panel_text = mock_panel.call_args[0][0]
                        self.assertNotIn("on #252538", panel_text)

    def test_bottom_toolbar_tips_no_ctrl_x(self) -> None:
        from coderai.cli.prompt_session import _TIPS

        self.assertNotIn("ctrl-x: toggle mode", _TIPS)
        self.assertTrue(any("shift-tab" in tip for tip in _TIPS))

    @unittest.skipUnless(HAS_PTK, "prompt_toolkit required")
    def test_shift_tab_toggles_plan_mode(self) -> None:
        callback_records = []

        def _cb(mode: bool) -> None:
            callback_records.append(mode)

        session = CoderAIPromptSession(
            ".", lambda: "gpt-5.6-luna", plan_mode=False, on_plan_mode_toggle=_cb
        )
        self.assertFalse(session.plan_mode)
        prompt_tokens = session._get_prompt_message()
        self.assertEqual(prompt_tokens, [("class:prompt", "❯ ")])

        # Simulate Shift-Tab keypress
        bindings = session._kb.get_bindings_for_keys(("s-tab",))
        self.assertTrue(len(bindings) > 0)
        handler = bindings[0].handler
        mock_event = type("Event", (), {"app": type("App", (), {"invalidate": lambda s: None})()})()

        # Toggle to Plan mode
        handler(mock_event)
        self.assertTrue(session.plan_mode)
        self.assertEqual(callback_records, [True])
        self.assertEqual(
            session._get_prompt_message(),
            [("class:prompt.plan", "[plan] "), ("class:prompt", "❯ ")],
        )

        # Toggle back to Build mode
        handler(mock_event)
        self.assertFalse(session.plan_mode)
        self.assertEqual(callback_records, [True, False])
        self.assertEqual(session._get_prompt_message(), [("class:prompt", "❯ ")])
