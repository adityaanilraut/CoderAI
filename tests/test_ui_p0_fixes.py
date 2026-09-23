"""P0 UI-review fixes: fail-closed approval, undo wiring, provider validation.

Covers the safety-critical findings from the terminal-UI deep dive:
- unknown approval keystrokes reprompt and never auto-allow
- /undo lists targets, restores via mgr.undo, and reports honestly
- unknown providers are rejected instead of writing bogus env keys
- partial --provider/--key setup errors instead of opening the wizard
- conflicting --preset/--tools-preset/--permission flags are rejected
- --agent/--agent-file are mutually exclusive
- /agents no longer shadows /agent; tree/report/send inspect live runs
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coderai.ui.shell.dispatch import ShellContext, SlashAction, cmd_agents, cmd_undo


def _ctx(mgr: MagicMock, session_id: str | None = "s1") -> ShellContext:
    return ShellContext(
        mgr=mgr,
        console=None,
        session_id=session_id,
        yes=False,
        ptk_session=None,
        pending_skills=[],
        active_plan_mode=False,
        turn_prompt=None,
    )


def test_approval_unknown_input_denies() -> None:
    """Unknown keystrokes reprompt; a following 'n' denies (never allows)."""
    from coderai.ui.shell import app as appmod

    req = [
        {
            "toolCallId": "tc1",
            "name": "write",
            "command": "",
            "scopes": ["write-in-cwd"],
            "risk_level": "MODERATE RISK",
        }
    ]
    inputs = iter(["bogus-xyz", "n"])
    with (
        patch.object(appmod.sys.stdin, "isatty", return_value=False),
        patch("builtins.input", side_effect=lambda *a, **k: next(inputs)),
    ):
        replies, _ = appmod._prompt_permissions(req, yes=False)
    assert replies[0]["permission"] == "deny"


def test_save_provider_api_key_rejects_unknown() -> None:
    """Unknown provider names raise instead of writing bogus env keys."""
    from coderai.config import save_provider_api_key

    with pytest.raises(ValueError, match="Unknown provider"):
        save_provider_api_key("bogusprovider", "sk-test-123", scope="user")


def test_setup_partial_provider_without_key_errors(tmp_path) -> None:
    """--provider without --key returns 1 and never opens the wizard."""
    from coderai.ui.shell.setup import run_setup_cli
    from coderai.ui.shell.startup import _build_parser

    args = _build_parser().parse_args(["--provider", "openai"])
    with patch("coderai.ui.shell.setup.run_setup_wizard") as wiz:
        assert run_setup_cli(args, project_root=str(tmp_path)) == 1
    assert not wiz.called


def test_conflicting_presets_rejected() -> None:
    """--preset + --permission with different values exits 1."""
    from coderai.ui.shell.app import main

    assert main(["--preset", "core", "--permission", "full", "--print", "-p", "hi"]) == 1


def test_agent_and_agent_file_mutually_exclusive() -> None:
    """--agent together with --agent-file exits 1."""
    from coderai.ui.shell.app import main

    assert main(["--agent", "default", "--agent-file", "x.yaml"]) == 1


def test_undo_restores_via_manager() -> None:
    """cmd_undo selects a target and calls mgr.undo with message_id+mode."""
    mgr = MagicMock()
    mgr.list_undo_targets.return_value = [
        {
            "index": 1,
            "message_id": "m1",
            "prompt": "hi",
            "checkpoint_hash": "abc",
            "can_restore_code": True,
        }
    ]
    mgr.undo.return_value = True
    with patch(
        "coderai.ui.shell.session_picker.select_undo_interactive",
        return_value=({"index": 1, "message_id": "m1"}, "restore_both"),
    ):
        assert cmd_undo(_ctx(mgr), "") == SlashAction.HANDLED
    mgr.undo.assert_called_once_with("s1", target_message_id="m1", mode="restore_both")


def test_undo_empty_targets_reports_no_crash() -> None:
    """No undoable turns prints a message and never calls mgr.undo."""
    mgr = MagicMock()
    mgr.list_undo_targets.return_value = []
    assert cmd_undo(_ctx(mgr), "") == SlashAction.HANDLED
    assert not mgr.undo.called


def test_agents_roles_delegates_and_tree_lists_live_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """'/agents roles' lists roles; '/agents tree' prints the live tree."""
    mgr = MagicMock()
    mgr.project_root = "."
    mgr.get_active_agent_role.return_value = "default"
    mgr.agent_registry = None
    with patch("coderai.subagents.registry.discover_markdown_agents", return_value=[]):
        assert cmd_agents(_ctx(mgr, None), "roles") == SlashAction.HANDLED
    assert cmd_agents(_ctx(mgr, None), "tree") == SlashAction.HANDLED
    captured = capsys.readouterr()
    assert "not yet implemented" not in captured.out
    assert "No live subagents" in captured.out
