"""Behavioral regressions for the terminal audit's unsafe workflows."""

from unittest.mock import AsyncMock, MagicMock, patch
import io

import pytest
from rich.console import Console

from coderai.ui.shell import app, session_picker
from coderai.ui.shell.dispatch import ShellContext, cmd_image, cmd_jobs
from coderai.ui.shell.prompt import _parse_line_range, expand_file_mentions
from coderai.ui.shell.visualize._approval_panel import prompt_plan_review


@pytest.mark.parametrize("cancel", [EOFError, KeyboardInterrupt, "esc", "q"])
def test_cancel_never_approves_or_restores(cancel):
    kwargs = {"side_effect": cancel} if isinstance(cancel, type) else {"return_value": cancel}
    with (
        patch("builtins.input", **kwargs),
        patch.object(session_picker.sys.stdin, "isatty", return_value=False),
    ):
        assert prompt_plan_review(None, "Implement the plan")["action"] == "reject"
        assert session_picker.select_undo_interactive(None, [{"message_id": "m1"}])[0] is None


@pytest.mark.parametrize("cancel", [EOFError, KeyboardInterrupt])
def test_question_cancel_does_not_fabricate_answer(cancel):
    with (
        patch("builtins.input", side_effect=cancel),
        patch.object(app.sys.stdin, "isatty", return_value=False),
    ):
        assert (
            app._prompt_user_questions([{"question": "Choose?", "options": [{"label": "First"}]}])
            is None
        )


@pytest.mark.parametrize("rich", [False, True])
def test_config_nested_credentials_are_redacted(rich, capsys):
    out = io.StringIO()
    console = Console(file=out, width=120) if rich else None
    settings = {
        "apiKey": "FAKE-TOP-SECRET",
        "providers": {"x": {"token": "FAKE-NESTED-SECRET"}},
        "oauth": {"access": "FAKE-OAUTH-SECRET"},
        "env": {"AWS_SECRET_ACCESS_KEY": "FAKE-ENV-SECRET"},
        "baseURL": "https://user:FAKE-URL-SECRET@host.test/?api_key=FAKE-QUERY-SECRET#access_token=FAKE-FRAGMENT-SECRET",
    }
    with patch("coderai.config.resolve_current_settings", return_value=settings):
        session_picker.render_config_interactive(console, ".")
    text = out.getvalue() + capsys.readouterr().out
    assert "FAKE-" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [None, "s1"])
async def test_image_first_request_carries_content(session):
    mgr = MagicMock(project_root=".")
    mgr.create_session = AsyncMock(return_value="s1")
    mgr.reply_session = AsyncMock()
    param = {"name": "file with spaces.png", "width": 1, "height": 1, "bytes": 24}
    with patch(
        "coderai.cli.image_attachment.parse_and_attach_image", return_value=(param, None)
    ) as attach:
        await cmd_image(
            ShellContext(mgr, session_id=session), '"file with spaces.png" inspect this'
        )
    attach.assert_called_once_with("file with spaces.png", ".")
    call = (mgr.reply_session if session else mgr.create_session).call_args
    assert call.kwargs["content_params"] == [param]
    mgr.append_message.assert_not_called()


def test_mentions_external_quoted_and_windows(tmp_path):
    external = tmp_path.parent / "external mention.txt"
    external.write_text("first\nsecond\nthird")
    text, files = expand_file_mentions(f'@"{external}":2-3', str(tmp_path))
    assert "second" in text and "third" in text
    assert files
    assert _parse_line_range(r"C:\Users\user\file.py:12-14") == (r"C:\Users\user\file.py", 12, 14)
    assert _parse_line_range(r"C:\Users\user\file.py") == (r"C:\Users\user\file.py", None, None)


def test_job_commands_scope_ownership(tmp_path, capsys):
    from coderai.background.store import JobStore

    store = JobStore()
    path = tmp_path / "log"
    path.write_text("hello\nlast")
    store.start(job_id="job", session_id="s1", kind="shell", label="test", output_path=str(path))
    ctx = ShellContext(MagicMock(job_store=store), session_id="other")
    cmd_jobs(ctx, "kill job")
    assert store.get("job", "s1").status == "running"
    ctx.session_id = "s1"
    cmd_jobs(ctx, "logs job")
    cmd_jobs(ctx, "kill job")
    cmd_jobs(ctx, "kill job")
    result = capsys.readouterr().out
    assert "hello" in result and "not-found" in result
    assert "cancellation-requested" in result and "already-finished" in result


def test_error_details_are_literal():
    with patch.object(app, "console", Console(file=io.StringIO())):
        app.error_callout(None, "Oops [red]", "[/white] [bold] external", hint="[/]")


@pytest.mark.parametrize("key", ["ESCAPE", "CTRL_C", "CTRL_D", ""])
def test_raw_dialog_cancellation_is_safe(key):
    console = Console(file=io.StringIO())
    with (
        patch.object(session_picker.sys.stdin, "isatty", return_value=True),
        patch.object(session_picker, "_read_single_key", return_value=key),
    ):
        assert prompt_plan_review(console, "Implement now")["action"] == "reject"
        assert session_picker.select_undo_interactive(console, [{"message_id": "m1"}])[0] is None


def test_raw_zero_match_enter_cannot_select():
    keys = iter([*"nonexistent-search", "ENTER", "CTRL_D"])
    with (
        patch.object(session_picker.sys.stdin, "isatty", return_value=True),
        patch.object(session_picker, "_read_single_key", side_effect=lambda: next(keys)),
    ):
        assert session_picker.select_with_arrows(None, [("first", "First option", "")]) is None


def test_setup_failed_probe_does_not_announce_readiness():
    from coderai.ui.shell import setup

    console = Console(file=io.StringIO())
    with (
        patch.object(setup, "select_with_arrows", return_value=1),
        patch.object(setup, "configure_provider_key_interactive", return_value=True),
        patch.object(setup, "select_and_save_model_interactive", return_value="gpt-6-sol"),
        patch.object(setup, "run_connectivity_test_interactive", return_value=False),
    ):
        setup.run_quick_setup_wizard(console, ".")
    assert "Setup complete" not in console.file.getvalue()
    assert "ready to start" not in console.file.getvalue()
