"""Approval UI + YOLO/AFK wiring.

Covers the safety-critical gaps:
- /yolo and /afk toggle SessionManager.set_yolo/set_afk (not a stray attribute)
- --yes auto-approves without persisting project always-allow scopes
- Plan Mode still prompts for mutating scopes even with YOLO
- ApprovalRequestPanel accepts session askPermissions dicts
- Numeric option 3 is Reject, not the feedback field
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from coderai.soul.approval import (
    Approval,
    ApprovalResult,
    apply_auto_approve_to_permission_plan,
)
from coderai.soul.session.manager import SessionManager
from coderai.ui.shell.dispatch import ShellContext, SlashAction, cmd_afk, cmd_yolo
from coderai.ui.shell.prompt import get_bottom_toolbar_tokens
from coderai.ui.shell.visualize._approval_panel import (
    ApprovalRequestPanel,
    permission_dict_to_request,
)


def _manager(tmp_path) -> SessionManager:
    return SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": None, "model": "gpt-4o"},
        get_resolved_settings=lambda: {"model": "gpt-4o"},
    )


def _ctx(mgr: SessionManager) -> ShellContext:
    return ShellContext(
        mgr=mgr,
        console=None,
        session_id="s1",
        yes=False,
        ptk_session=None,
        pending_skills=[],
        active_plan_mode=False,
        turn_prompt=None,
    )


def _write_request() -> list[dict]:
    return [
        {
            "toolCallId": "tc1",
            "name": "write",
            "command": "write test.py",
            "scopes": ["write-in-cwd"],
            "risk_level": "MODERATE RISK",
            "description": "Update test.py",
            "diff_preview": "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n-old\n+new",
        }
    ]


def test_session_manager_yolo_property_drives_auto_approve(tmp_path) -> None:
    mgr = _manager(tmp_path)
    assert mgr.is_yolo() is False
    assert mgr.is_auto_approve() is False
    mgr.yolo = True
    assert mgr.is_yolo() is True
    assert mgr.is_auto_approve() is True
    mgr.set_yolo(False)
    assert mgr.yolo is False
    assert mgr.is_auto_approve() is False


def test_session_manager_afk_property_drives_auto_approve(tmp_path) -> None:
    mgr = _manager(tmp_path)
    mgr.afk = True
    assert mgr.is_afk() is True
    assert mgr.is_auto_approve() is True
    mgr.set_afk(False)
    assert mgr.afk is False


def test_cmd_yolo_toggles_manager_and_context(tmp_path) -> None:
    mgr = _manager(tmp_path)
    ctx = _ctx(mgr)
    assert cmd_yolo(ctx, "") == SlashAction.HANDLED
    assert mgr.is_yolo() is True
    assert ctx.yes is True
    assert cmd_yolo(ctx, "") == SlashAction.HANDLED
    assert mgr.is_yolo() is False
    assert ctx.yes is False


def test_cmd_afk_toggles_manager_and_context(tmp_path) -> None:
    mgr = _manager(tmp_path)
    ctx = _ctx(mgr)
    assert cmd_afk(ctx, "") == SlashAction.HANDLED
    assert mgr.is_afk() is True
    assert ctx.yes is True
    assert cmd_afk(ctx, "") == SlashAction.HANDLED
    assert mgr.is_afk() is False
    assert ctx.yes is False


def test_permission_dict_to_request_renders_panel() -> None:
    req = permission_dict_to_request(_write_request()[0], plan_mode_forced=True)
    assert req.sender == "write"
    assert "write test.py" in req.action
    panel = ApprovalRequestPanel(req, allow_session_approve=True)
    rendered = str(panel.render(blocking_keys=True))
    assert "approval" in rendered.lower() or "Approve once" in str(panel.options[0])
    assert panel.options[1][1] == "approve_for_session"
    assert panel.options[2][1] == "reject"
    assert panel.is_feedback_selected is False
    panel.selected_index = 2
    assert panel.get_selected_response() == "reject"
    panel.selected_index = panel.FEEDBACK_OPTION_INDEX
    assert panel.is_feedback_selected is True


def test_approval_panel_hides_session_approve_in_plan_mode() -> None:
    req = permission_dict_to_request(_write_request()[0], plan_mode_forced=True)
    panel = ApprovalRequestPanel(req, allow_session_approve=False)
    assert all(kind != "approve_for_session" for _, kind in panel.options)
    assert panel.options[1][1] == "reject"
    panel.selected_index = 1
    assert panel.get_selected_response() == "reject"
    assert panel.FEEDBACK_OPTION_INDEX == 2


def test_prompt_permissions_numeric_reject_is_not_feedback() -> None:
    """Key 3 is Reject when the panel has four options; it must not open feedback."""
    from coderai.ui.shell import app as appmod

    with (
        patch.object(appmod.sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["3"]),
        patch.object(appmod.console, "print"),
    ):
        replies, always = appmod._prompt_permissions(_write_request(), yes=False)
    assert replies[0]["permission"] == "deny"
    assert "feedback" not in replies[0]
    assert always == []


def test_prompt_permissions_numeric_feedback_is_option_four() -> None:
    from coderai.ui.shell import app as appmod

    with (
        patch.object(appmod.sys.stdin, "isatty", return_value=True),
        patch("builtins.input", side_effect=["4", "please use a smaller patch"]),
        patch.object(appmod.console, "print"),
    ):
        replies, _ = appmod._prompt_permissions(_write_request(), yes=False)
    assert replies[0]["permission"] == "deny"
    assert replies[0]["feedback"] == "please use a smaller patch"


def test_prompt_permissions_plan_mode_still_prompts_with_yes() -> None:
    from coderai.ui.shell import app as appmod

    with (
        patch.object(appmod.sys.stdin, "isatty", return_value=False),
        patch("builtins.input", return_value="n"),
    ):
        replies, _ = appmod._prompt_permissions(
            _write_request(), yes=True, plan_mode=True
        )
    assert replies[0]["permission"] == "deny"


def test_apply_auto_approve_keeps_plan_mode_mutating_asks() -> None:
    plan = {
        "permissions": [
            {"toolCallId": "r1", "permission": "ask"},
            {"toolCallId": "w1", "permission": "ask"},
        ],
        "askPermissions": [
            {"toolCallId": "r1", "scopes": ["read-in-cwd"], "name": "read"},
            {"toolCallId": "w1", "scopes": ["write-in-cwd"], "name": "write"},
        ],
    }
    out = apply_auto_approve_to_permission_plan(plan, plan_mode=True)
    assert out is not None
    by_id = {item["toolCallId"]: item["permission"] for item in out["permissions"]}
    assert by_id["r1"] == "allow"
    assert by_id["w1"] == "ask"
    remaining = {item["toolCallId"] for item in out["askPermissions"]}
    assert remaining == {"w1"}


def test_apply_auto_approve_allows_all_outside_plan_mode() -> None:
    plan = {
        "permissions": [{"toolCallId": "w1", "permission": "ask"}],
        "askPermissions": [{"toolCallId": "w1", "scopes": ["write-in-cwd"], "name": "write"}],
    }
    out = apply_auto_approve_to_permission_plan(plan, plan_mode=False)
    assert out is not None
    assert out["askPermissions"] is None
    assert out["permissions"][0]["permission"] == "allow"


def test_approval_request_respects_manager_yolo(tmp_path) -> None:
    mgr = _manager(tmp_path)
    mgr.set_yolo(True)
    approval = Approval()

    async def _run() -> ApprovalResult:
        return await approval.request("write", "edit", "write file")

    result = asyncio.run(_run())
    assert bool(result) is True


def test_toolbar_tokens_include_yolo_and_afk() -> None:
    tokens = get_bottom_toolbar_tokens(
        project_root="/tmp",
        plan_mode=False,
        active_model="gpt-4o",
        yolo=True,
        afk=True,
    )
    text = "".join(part[1] for part in tokens)
    assert "yolo" in text
    assert "afk" in text


def test_structured_diff_preview_does_not_recurse() -> None:
    from coderai.tools.display import DiffDisplayBlock
    from coderai.utils.rich.diff_render import collect_diff_hunks, render_diff_preview

    blocks = [DiffDisplayBlock(path="foo.py", old_text="old\n", new_text="new\n")]
    hunks, added, removed = collect_diff_hunks(blocks)
    renderables, remaining = render_diff_preview("foo.py", hunks, added, removed)
    assert isinstance(renderables, list)
    assert renderables
    assert remaining == 0


def test_resume_restores_yolo_from_session_state(tmp_path) -> None:
    from coderai.session_state import SessionState, save_session_state

    mgr = _manager(tmp_path)
    sid = "sess_resume_yolo"
    session_dir = mgr._session_dir(sid)
    session_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState()
    state.approval.yolo = True
    save_session_state(state, session_dir)

    resumed = _manager(tmp_path)
    loaded = resumed.get_session_state(sid)
    assert resumed.is_yolo() is True
    assert loaded.approval.yolo is True


def test_print_mode_denies_plan_mode_mutations() -> None:
    from coderai.ui.print import _permission_replies_for_print

    requests = [
        {"toolCallId": "r1", "scopes": ["read-in-cwd"], "name": "read"},
        {"toolCallId": "w1", "scopes": ["write-in-cwd"], "name": "write"},
    ]
    replies = _permission_replies_for_print(requests, plan_mode=True)
    by_id = {item["toolCallId"]: item["permission"] for item in replies}
    assert by_id["r1"] == "allow"
    assert by_id["w1"] == "deny"


def test_questions_to_request_builds_panel() -> None:
    from coderai.ui.shell.visualize._question_panel import (
        QuestionRequestPanel,
        questions_to_request,
    )

    req = questions_to_request(
        [
            {
                "question": "Which approach?",
                "options": [{"label": "A", "description": "fast"}],
                "multiSelect": False,
            }
        ]
    )
    panel = QuestionRequestPanel(req)
    assert panel.current_question_text == "Which approach?"
    assert panel._current_question.multi_select is False
    rendered = panel.render()
    assert rendered is not None
