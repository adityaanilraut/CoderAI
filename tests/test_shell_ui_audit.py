"""Regression journeys for durable intent, informed decisions, and terminal navigation."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from coderai.soul.session.models import SessionEntry, SessionMessage
from coderai.ui.shell.browsers import diff_rows, session_rows, restore_checkpoint
from coderai.ui.shell.controller import ShellController, ShellViewState
from coderai.ui.shell.interaction import BrowserRow, choose
from coderai.ui.shell.placeholders import PromptPlaceholderManager
from coderai.ui.shell.preferences import DisplayPreferences
from coderai.ui.shell.presentation import ShellPresentation
from coderai.ui.shell.storage import ShellStorage
from coderai.ui.shell.submission import PromptSubmission, SelectorOutcome


@pytest.fixture(autouse=True)
def private_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CODERAI_SHARE_DIR", str(tmp_path / "private"))


@pytest.fixture
def shell(tmp_path):
    mgr = MagicMock(project_root=str(tmp_path))
    mgr.get_resolved_settings.return_value = {"contextWindow": 32000, "multimodal": "on"}
    mgr.get_active_model.return_value = "gpt-6-sol"
    mgr.get_active_agent_role.return_value = "default"
    mgr.is_auto_approve.return_value = mgr.is_afk.return_value = False
    mgr.get_session.return_value = SessionEntry("s1", status="ready")
    mgr.create_session = AsyncMock()
    mgr.reply_session = AsyncMock()
    with patch.object(DisplayPreferences, "load", return_value=DisplayPreferences()):
        result = ShellController(
            mgr, Console(file=io.StringIO()), prompt=MagicMock(shell_mode=False)
        )
    result.view.session_id = "s1"
    return result


def test_paste_survives_restart_without_aliasing_new_or_legacy_tokens():
    first = PromptPlaceholderManager()
    token = first.maybe_placeholderize_pasted_text("original " * 300)
    restarted = PromptPlaceholderManager()
    other = restarted.maybe_placeholderize_pasted_text("different " * 300)
    assert token != other
    assert restarted.resolve_command(token).resolved_text == "original " * 300
    assert restarted.resolve_command("[Pasted text #1]").resolved_text == "[Pasted text #1]"


def test_recovery_is_private_workspace_scoped_and_queues_are_paused(tmp_path):
    store = ShellStorage(str(tmp_path / "one"))
    view = ShellViewState(
        session_id="s1",
        draft="unsent",
        cursor=3,
        attachments=[{"name": "img", "bytes": 42}],
        queue=[PromptSubmission("queued", "resolved", files=["one.py"])],
    )
    store.save_view(view)
    recovered = ShellStorage(str(tmp_path / "one")).load_view("s1")
    assert recovered["draft"] == "unsent" and recovered["cursor"] == 3
    assert recovered["attachments"] == view.attachments
    assert recovered["queue"][0].files == ["one.py"] and recovered["queue_paused"]
    assert ShellStorage(str(tmp_path / "two")).load_view("s1") == {}
    assert (store.folder / store.view_name("s1")).stat().st_mode & 0o077 == 0


def test_stale_autosave_cannot_overwrite_newer_input_or_deleted_session(tmp_path):
    store = ShellStorage(str(tmp_path))
    view = ShellViewState(session_id="s1", draft="new")
    store.save_view(view, revision=20)
    store.save_view(view, draft="old", revision=10)
    assert store.load_view("s1")["draft"] == "new"
    store.delete_view("s1")
    store.save_view(view, revision=20)
    assert store.load_view("s1") == {}


@pytest.mark.asyncio
async def test_recovered_cursor_at_start_reaches_composer(shell):
    shell.view.draft, shell.view.cursor = "unsent", 0
    shell.persist()
    shell.view = shell.recover("s1")
    shell.prompt.prompt_async = AsyncMock(side_effect=EOFError)
    shell.drain = AsyncMock()
    shell.refresh = AsyncMock()
    with patch("coderai.cli.exit_summary.render_exit_summary"):
        await shell.run()
    shell.prompt.prompt_async.assert_awaited_once_with(default="unsent", cursor=0)


@pytest.mark.asyncio
async def test_new_prompt_never_resumes_paused_queue(shell):
    shell.view.queue = [PromptSubmission("old", "old")]
    shell.queue_paused = True
    await shell.handle("replacement")
    await shell.active
    await shell.settle()
    assert shell.queue_paused and shell.view.queue[0].display_text == "old"
    assert "paused:1" in shell.stats()["pending"]
    await shell.handle("/queue run")
    assert not shell.queue_paused


@pytest.mark.asyncio
async def test_queue_edit_preserves_images_and_order_and_pauses(shell):
    image = {"name": "sample", "bytes": 1}
    shell.view.queue = [
        PromptSubmission("first", "first"),
        PromptSubmission("edit me", "expanded", attachments=[image], skills=["review"]),
    ]
    await shell.handle("/queue edit 2")
    assert shell.view.draft == "edit me"
    assert shell.view.attachments == [image] and shell.pending_skills == ["review"]
    assert len(shell.view.queue) == 1 and shell.queue_paused
    recovered = shell.storage.load_view("s1")
    assert recovered["draft"] == "edit me" and recovered["attachments"] == [image]


@pytest.mark.asyncio
async def test_invalid_file_submission_returns_draft(shell):
    draft = "explain @missing-file-unique.py"
    with pytest.raises(ValueError):
        await shell.handle(draft)
    assert shell.view.draft == draft
    assert shell.storage.load_view("s1")["draft"] == draft


@pytest.mark.asyncio
async def test_accessible_decision_shows_details_and_rejects_disabled(capsys):
    prompt = SimpleNamespace(prompt_async=AsyncMock(side_effect=["2", "1"]))
    result = await choose(
        prompt,
        "Approve command",
        [
            BrowserRow("allow", "Allow once", "Command: touch file; scope: workspace"),
            BrowserRow("blocked", "Unavailable", "Cannot restore files", disabled=True),
        ],
        accessible=True,
    )
    assert result.value == "allow"
    output = capsys.readouterr().out
    assert "Command: touch file; scope: workspace" in output
    assert "This action is unavailable" in output


@pytest.mark.asyncio
async def test_multi_selection_marks_and_toggles_answers(shell):
    shell.select = AsyncMock(
        side_effect=[
            SelectorOutcome("0"),
            SelectorOutcome("1"),
            SelectorOutcome("0"),
            SelectorOutcome("done"),
        ]
    )
    outcome = await shell.questions(
        [
            {
                "question": "Choose",
                "multiSelect": True,
                "options": [{"label": "Red"}, {"label": "Blue"}],
            }
        ]
    )
    assert outcome.text == "Choose: Blue"
    rows = shell.select.call_args_list[1].args[1]
    assert rows[0].label == "[x] Red" and rows[1].label == "[ ] Blue"
    assert rows[-1].label == "Submit 1 selected answers"
    assert shell.select.call_args_list[0].args[1][-1].disabled


def test_complete_diff_counts_and_navigation_do_not_truncate():
    text = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,600 @@\n" + "\n".join(
        f"+line {i}" for i in range(600)
    )
    rows = diff_rows(text)
    assert rows[0].label == "All changes: +600 -0"
    assert rows[-1].id == "hunk:0:0" and "+line 599" in rows[-1].detail
    from coderai.utils.rich.diff_render import render_diff_preview

    output = io.StringIO()
    render_diff_preview(Console(file=output), text)
    assert "+600" in output.getvalue()


@pytest.mark.asyncio
async def test_restore_exposes_impact_and_requires_confirmation(shell):
    shell.mgr.list_undo_targets.return_value = [
        {
            "message_id": "u",
            "prompt": "before",
            "can_restore_code": False,
            "create_time": "yesterday",
        }
    ]
    shell.mgr.list_session_messages.return_value = [
        SessionMessage("u", "s1", "user", "before"),
        SessionMessage("a", "s1", "assistant", "after"),
    ]
    shell.select = AsyncMock(
        side_effect=[
            SelectorOutcome("0"),
            SelectorOutcome("restore_conversation_only"),
            SelectorOutcome("cancel"),
        ]
    )
    await restore_checkpoint(shell)
    scope_rows = shell.select.call_args_list[1].args[1]
    assert scope_rows[1].disabled and scope_rows[2].disabled
    assert "removes 1 messages" in scope_rows[0].detail
    shell.mgr.undo.assert_not_called()
    shell.select.side_effect = [
        SelectorOutcome("0"),
        SelectorOutcome("restore_conversation_only"),
        SelectorOutcome("restore"),
    ]
    await restore_checkpoint(shell)
    shell.mgr.undo.assert_called_once_with(
        "s1", target_message_id="u", mode="restore_conversation_only"
    )


def test_session_filters_pin_archive_and_fork_tree():
    entries = [
        SessionEntry("root", "Root", status="ready", update_time="2026-01-01"),
        SessionEntry("child", "Child", status="failed", update_time="2026-02-01", fork_of="root"),
    ]
    library = {"sessions": {"root": {"pinned": True}, "child": {"archived": True}}}
    assert [r.id for r in session_rows(entries, library, "")[0]] == ["root"]
    assert [
        r.id
        for r in session_rows(entries, library, "archived:only status:failed after:2026-01-15")[0]
    ] == ["child"]
    tree = session_rows(entries, library, "archived:all fork:root tree")[0]
    assert [r.id for r in tree] == ["root", "child"]
    assert "+-" in tree[1].label and "Children: child" in tree[0].detail


@pytest.mark.asyncio
async def test_session_organization_and_favorites_persist(shell):
    await shell.handle("/sessions pin")
    await shell.handle("/sessions archive")
    await shell.handle("/model favorite")
    data = ShellStorage(shell.mgr.project_root).library()
    assert data["sessions"]["s1"] == {"pinned": True, "archived": True}
    assert data["favorite_models"]["gpt-6-sol"]
    await shell.handle("/model unfavorite")
    assert not shell.storage.library()["favorite_models"]["gpt-6-sol"]


def test_stream_commits_once_and_tool_preview_bounds_single_line():
    output = io.StringIO()
    p = ShellPresentation(Console(file=output), DisplayPreferences(), lambda: None)
    p.switch("s1")
    full = "First paragraph.\n\nSecond paragraph.\n\nLast paragraph."
    p.chunk(full)
    assert "First paragraph" in output.getvalue()
    p.message(SessionMessage("a", "s1", "assistant", full))
    assert output.getvalue().count("First paragraph") == 1
    assert output.getvalue().count("Last paragraph") == 1
    output.truncate(0)
    output.seek(0)
    import json

    p.message(
        SessionMessage(
            "t",
            "s1",
            "tool",
            json.dumps({"name": "bash", "ok": True, "output": "x" * 100000}),
            tool_call_id="tool-1",
        )
    )
    assert len(output.getvalue()) < 2500 and "/output tool-1" in output.getvalue()
    assert len(p.outputs.items["tool-1"][1]) > 100000


@pytest.mark.parametrize("width", [40, 60, 80])
def test_welcome_keeps_status_and_model_readable(tmp_path, width):
    from coderai.ui.shell.welcome import render_welcome_screen

    output = io.StringIO()
    with (
        patch("coderai.config.resolve_current_settings", return_value={}),
        patch("coderai.llm.resolve_model_provider_routing", return_value=("endpoint", "key")),
    ):
        render_welcome_screen(Console(file=output, width=width), str(tmp_path), "gpt-6-sol")
    text = output.getvalue()
    assert "not verified" in text and "gpt-6-sol" in text
    from coderai.cli.ascii_art import CODERAI_ASCII_LOGO

    if width >= 58:
        assert all(line in text for line in CODERAI_ASCII_LOGO)
    else:
        assert "CoderAI" in text and "█" not in text


def test_model_metadata_never_probes_and_key_changes_invalidate_verification(tmp_path):
    from coderai.ui.shell.model_details import connection_label, describe_models, record_connection

    root = str(tmp_path)
    with patch(
        "coderai.llm.resolve_model_provider_routing",
        side_effect=lambda model, **kw: ("endpoint", kw.get("explicit_api_key")),
    ):
        record_connection(root, "test", {"apiKey": "one"}, True)
        assert connection_label(root, "test", {"apiKey": "one"}).startswith("Verified")
        assert "not verified" in connection_label(root, "test", {"apiKey": "two"})
        with patch("coderai.openrouter.fetch_openrouter_models", return_value=[]) as fetch:
            models = describe_models(
                {"custom": [("test", "Custom model", "")]}, root, {"apiKey": "one"}, "test"
            )
            fetch.assert_called_once_with(allow_network=False)
        assert all(
            label in models["custom"][0][1]
            for label in ("Verified", "Images: unknown", "Tools: unknown", "Pricing: Unavailable")
        )
    assert "one" not in (ShellStorage(root).folder / "library.json").read_text()


@pytest.mark.asyncio
async def test_permission_picker_matches_runtime_and_marks_current(shell):
    from coderai.sandbox import SANDBOX_MODES

    shell.mgr.get_resolved_settings.return_value = {"permissions": {"sandbox": "workspace-write"}}
    shell.select = AsyncMock(return_value=SelectorOutcome())
    await shell.handle("/permission")
    rows = shell.select.call_args.args[1]
    assert [r.id for r in rows] == list(SANDBOX_MODES)
    assert "current" in rows[1].label and "deny:" in rows[1].detail


@pytest.mark.asyncio
async def test_activity_contains_tools_todos_goal_budget(shell):
    from coderai.ui.shell.controller_commands import browse_activity
    from coderai.goals.core import get_goal_store

    get_goal_store(shell.mgr.project_root).create("s1", "Fix bug", status="pending")
    shell.presentation.active_tools = {"t1": "bash"}
    shell.presentation.todos = [{"content": "Run checks", "status": "in_progress"}]
    captured = []

    async def select(title, source):
        captured.extend(source())
        return SelectorOutcome()

    shell.select = select
    await browse_activity(shell)
    assert "Active tools: bash" in captured[0].detail
    assert any("Run checks" in r.label for r in captured)
    assert any("Rounds: 0/20" in r.detail for r in captured)


@pytest.mark.asyncio
async def test_mcp_browser_reconnects_selected_server(shell):
    from coderai.ui.shell.browsers import browse_integrations

    shell.mgr.mcp_manager.get_status.return_value = [
        SimpleNamespace(
            name="demo",
            status="failed",
            error="Disconnected",
            tool_count=0,
            prompt_count=0,
            resource_count=0,
        )
    ]
    shell.mgr.mcp_manager.list_tools.return_value = []
    shell.mgr.mcp_manager.reconnect = AsyncMock(return_value=True)
    shell.select = AsyncMock(
        side_effect=[
            SelectorOutcome("server:demo"),
            SelectorOutcome("reconnect"),
            SelectorOutcome(),
        ]
    )
    await browse_integrations(shell)
    shell.mgr.mcp_manager.reconnect.assert_awaited_once_with("demo")
    shell.mgr._refresh_mcp_tool_definitions.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_token_auth_uses_private_project_scoped_reference(shell):
    from pathlib import Path
    from coderai.config import read_project_settings
    from coderai.ui.shell.browsers import authenticate_integration

    shell.mgr.mcp_manager.server_configs = {
        "demo": {"url": "https://example.test/mcp", "tokenEnv": "OLD_TOKEN"}
    }
    shell.mgr.mcp_manager.reconnect = AsyncMock(return_value=True)
    with patch("getpass.getpass", return_value="private-test-token"):
        await authenticate_integration(shell, "demo")
    settings = read_project_settings(shell.mgr.project_root)
    config = settings["mcpServers"]["demo"]
    assert "private-test-token" not in str(settings) and "tokenEnv" not in config
    token_file = Path(config["tokenFile"])
    assert token_file.stat().st_mode & 0o077 == 0
    assert "private-test-token" in token_file.read_text()
    shell.mgr.mcp_manager.reconnect.assert_awaited_once_with("demo", config)
    with patch("getpass.getpass", return_value=""):
        await authenticate_integration(shell, "demo")
    assert shell.mgr.mcp_manager.reconnect.await_count == 1
