"""Offline lifecycle, layout, attachment and accounting interaction regressions."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from coderai.soul.session.models import SessionEntry, SessionMessage
from coderai.ui.shell.controller import ShellController, ShellState
from coderai.ui.shell.interaction import BrowserRow, choose, filter_rows, inspect_output
from coderai.ui.shell.preferences import DisplayPreferences
from coderai.ui.shell.prompt import get_bottom_toolbar_tokens
from coderai.ui.shell.runtime_view import context_limit, session_cost
from coderai.ui.shell.submission import PromptSubmission


@pytest.fixture
def controller(tmp_path):
    mgr = MagicMock(project_root=str(tmp_path))
    mgr.get_resolved_settings.return_value = {"contextWindow": 32000, "multimodal": "on"}
    mgr.get_active_model.return_value = "gpt-6-sol"
    mgr.is_auto_approve.return_value = False
    mgr.is_afk.return_value = False
    mgr.get_session.return_value = SessionEntry("s1", status="ready")
    mgr.create_session = AsyncMock()
    mgr.reply_session = AsyncMock()
    prompt = MagicMock(shell_mode=False)
    prompt.session.app.output.get_size.return_value = SimpleNamespace(rows=24, columns=80)
    with patch.object(DisplayPreferences, "load", return_value=DisplayPreferences()):
        result = ShellController(mgr, Console(file=io.StringIO()), prompt=prompt)
    return result


@pytest.mark.asyncio
async def test_queue_and_steer_while_running_exactly_once(controller):
    gate = asyncio.Event()

    async def delayed(*args, **kwargs):
        await gate.wait()

    controller.mgr.create_session.side_effect = delayed
    await controller.handle("first")
    await asyncio.sleep(0)
    await controller.handle("second")
    await controller.handle("change direction", steer=True)
    assert controller.running and len(controller.view.queue) == 1
    controller.mgr.steer_session.assert_called_once_with(
        controller.view.session_id, "change direction"
    )
    controller.mgr.reply_session.assert_not_called()
    gate.set()
    await controller.active
    await controller.settle()
    controller.start(controller.view.queue.pop(0))
    await controller.active
    await controller.settle()
    assert controller.mgr.create_session.await_count == 1
    assert controller.mgr.reply_session.await_count == 1
    assert controller.mgr.reply_session.call_args.args[1] == "second"


@pytest.mark.asyncio
async def test_stop_settles_and_retry_keeps_images(controller):
    async def delayed(*args, **kwargs):
        await asyncio.Event().wait()

    controller.mgr.create_session.side_effect = delayed
    submission = PromptSubmission(
        "image", "inspect", attachments=[{"image_url": {"url": "data:image/png;base64,fixture"}}]
    )
    controller.start(submission)
    await asyncio.sleep(0)
    assert controller.stop()
    await controller.settle()
    assert controller.view.phase == ShellState.IDLE
    assert controller.view.last_failed is submission
    assert controller.queue_paused
    await controller.handle("/retry")
    await controller.active
    await controller.settle()
    assert controller.mgr.reply_session.call_args.kwargs["content_params"] == submission.attachments


@pytest.mark.asyncio
async def test_approval_cancel_and_question_cancel_never_reply(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.view.session_id = "s1"
    entry = SessionEntry(
        "s1", status="ask_permission", ask_permissions=[{"toolCallId": "t1", "name": "write"}]
    )
    controller.mgr.get_session.return_value = entry
    controller.select = AsyncMock(return_value=SelectorOutcome())
    await controller.drain()
    controller.mgr.reply_session.assert_not_called()
    controller.mgr.interrupt_session.assert_called_once_with("s1")
    result = await controller.questions([{"question": "Which?", "options": [{"label": "Default"}]}])
    assert result.status == "cancelled" and result.text is None


@pytest.mark.asyncio
async def test_approval_continuation_returns_composer_ownership(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.view.session_id = "s1"
    controller.mgr.get_session.return_value = SessionEntry(
        "s1", status="ask_permission", ask_permissions=[{"toolCallId": "t1", "name": "write"}]
    )
    controller.select = AsyncMock(return_value=SelectorOutcome("once"))
    await controller.drain()
    assert controller.active is not None
    await controller.active
    assert controller.mgr.reply_session.call_args.kwargs["permission_replies"] == [
        {"toolCallId": "t1", "permission": "allow"}
    ]


@pytest.mark.asyncio
async def test_queue_order_and_session_scope(controller):
    controller.view.session_id = "s1"
    controller.view.draft = "keep draft"
    controller.view.queue = [PromptSubmission(str(i), str(i)) for i in range(3)]
    await controller.handle("/queue move 1 3")
    assert [x.resolved_text for x in controller.view.queue] == ["1", "2", "0"]
    await controller.handle("/queue remove 2")
    controller.presentation.outputs.retain("old", "old", "secret output")
    controller.switch("s2")
    assert not controller.view.queue and not controller.presentation.outputs.items
    controller.switch("s1")
    assert controller.view.draft == "keep draft"
    assert [x.resolved_text for x in controller.view.queue] == ["1", "0"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/new", "/sessions", "/undo", "/fork", "/reset"])
async def test_session_navigation_never_deletes(controller, command):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.view.session_id = "s1"
    controller.mgr.list_sessions.return_value = [SessionEntry("s2", summary="Older session")]
    controller.mgr.fork_session.return_value = "s2"
    controller.select = AsyncMock(return_value=SelectorOutcome("s2"))
    controller.undo = AsyncMock()
    await controller.handle(command)
    controller.mgr.delete_session.assert_not_called()
    expected = None if command in ("/new", "/reset") else "s1" if command == "/undo" else "s2"
    assert controller.view.session_id == expected


@pytest.mark.asyncio
async def test_delete_requires_explicit_selection_and_only_deletes_target(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.view.session_id = "s1"
    controller.select = AsyncMock(return_value=SelectorOutcome())
    await controller.handle("/delete")
    controller.mgr.delete_session.assert_not_called()
    controller.select.return_value = SelectorOutcome("delete")
    controller.mgr.delete_session.return_value = True
    await controller.handle("/delete")
    controller.mgr.delete_session.assert_called_once_with("s1")
    assert controller.view.session_id is None


@pytest.mark.asyncio
async def test_long_slash_command_is_owned_and_cancellable(controller):
    controller.view.session_id = "s1"
    entered = asyncio.Event()

    async def delayed(*args):
        entered.set()
        await asyncio.Event().wait()

    with patch("coderai.ui.shell.dispatch.dispatch_slash_command", side_effect=delayed):
        await controller.handle("/review --all")
        await entered.wait()
        await controller.handle("follow-up")
        assert controller.running and len(controller.view.queue) == 1
        with pytest.raises(ValueError, match="cannot accept steering"):
            await controller.handle("change", steer=True)
        assert controller.stop()
        await controller.settle()
        assert controller.view.last_failed.action == "command"
        assert controller.queue_paused


@pytest.mark.asyncio
async def test_session_event_observer_scopes_retries_and_closes(controller):
    from coderai.wire.emitter import WireEmitter
    from coderai.wire.types import StepRetry, CompactionBegin, CompactionEnd

    emitters = {"s1": WireEmitter(), "s2": WireEmitter()}
    controller.mgr.get_event_emitter.side_effect = emitters.__getitem__
    controller.view.session_id = "s1"
    controller.observe_session("s1")
    first = controller.event_task
    controller.presentation.pending = "partial failed response"
    emitters["s1"].send(StepRetry(next_attempt=2, max_attempts=6, wait_s=0.5, error_type="Timeout"))
    await asyncio.sleep(0)
    assert controller.presentation.pending == ""
    assert "Retry 2/6" in controller.console.file.getvalue()
    controller.switch("s2")
    emitters["s1"].send(StepRetry(error_type="unrelated retry"))
    emitters["s2"].send(CompactionBegin())
    emitters["s2"].send(CompactionEnd())
    await asyncio.sleep(0)
    assert "unrelated retry" not in controller.console.file.getvalue()
    assert "Context compaction complete" in controller.console.file.getvalue()
    controller.observe_session(None)
    await asyncio.gather(first, *controller.events, return_exceptions=True)
    await asyncio.sleep(0)
    assert not controller.events
    for emitter in emitters.values():
        emitter.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_home")
@pytest.mark.parametrize("failure", ["empty", "transport"])
async def test_engine_retry_publishes_actual_retry_metadata(tmp_path, monkeypatch, failure):
    from coderai.cli.session_factory import close_session_manager
    from coderai.soul.session.manager import SessionManager
    from coderai.wire.types import StepRetry

    mgr = SessionManager(
        project_root=str(tmp_path),
        create_openai_client=lambda: {"client": object()},
        get_resolved_settings=lambda: {"model": "gpt-4o", "contextWindow": 4000},
    )
    calls = 0

    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "transport":
                raise ConnectionError("connection reset")
            return {"choices": [{"message": {"content": ""}}]}
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr("coderai.soul.session.manager.retry_delay_ms", lambda _: 0)
    mgr._create_completion = complete
    try:
        await mgr._create_completion_with_retry(
            "retry", object(), {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        )
        retries = [e for e in mgr.get_event_emitter("retry").buffered() if isinstance(e, StepRetry)]
        assert calls == 2 and len(retries) == 1
        assert retries[0].next_attempt == 2 and retries[0].wait_s == 0
        assert retries[0].error_type == (
            "empty response" if failure == "empty" else "ConnectionError"
        )
    finally:
        await close_session_manager(mgr)


def test_reasoning_is_inspectable_and_verbose_commit_is_literal(controller):
    controller.presentation.switch("s1")
    controller.presentation.thinking_chunk("reasoning [/white]")
    assert controller.presentation.preview(24, 80) == [("class:toolbar", "Thinking...\n")]
    controller.preferences.detail = "verbose"
    message = SessionMessage("m1", "s1", "assistant", "Answer", thinking="reasoning [/white]")
    controller.presentation.message(message)
    controller.presentation.message(message)
    assert controller.console.file.getvalue().count("reasoning [/white]") == 1
    controller.file_preview = ["attached file: 123 bytes"]
    controller.view.attachments = [{"file_path": "image.png", "bytes": 123}]
    controller.preferences.accessible = True
    assert controller.preview() == []


@pytest.mark.asyncio
async def test_focused_browser_defers_scrollback_and_editor_restores_terminal(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    async def focused(*args, **kwargs):
        controller.presentation.emit("background finished")
        assert "background finished" not in controller.console.file.getvalue()
        return SelectorOutcome()

    with patch("coderai.ui.shell.controller.choose", side_effect=focused):
        await controller.select("Activity", [])
    assert controller.console.file.getvalue().count("background finished") == 1

    def edit(text):
        controller.presentation.emit("finished while editing")
        assert "finished while editing" not in controller.console.file.getvalue()
        return "edited draft"

    with patch("coderai.utils.editor.open_external_editor", side_effect=edit):
        await controller.handle("/editor")
    cooked = controller.prompt.session.app.input.cooked_mode.return_value
    cooked.__enter__.assert_called_once()
    cooked.__exit__.assert_called_once()
    assert controller.view.draft == "edited draft"
    assert controller.console.file.getvalue().count("finished while editing") == 1


@pytest.mark.asyncio
async def test_attachment_tray_preserves_draft_and_previews_resolved_files(controller, tmp_path):
    from coderai.ui.shell.submission import SelectorOutcome

    file = tmp_path / "unicode λ file.py"
    file.write_text("line 1\nline 2\n")
    controller.view.draft = 'Explain @"unicode λ file.py":1-2 in detail'
    image = {"name": "image.png", "bytes": 24, "width": 2, "height": 3}
    controller.view.attachments = [image]
    controller.select = AsyncMock(return_value=SelectorOutcome("1"))
    await controller.handle("/attachments edit")
    rows = controller.select.call_args.args[1]
    assert str(file) in rows[1].detail
    assert "lines 1-2" in rows[1].detail and "bytes" in rows[1].detail
    assert "2 x 3" in rows[0].detail
    assert controller.view.draft == "Explain  in detail"
    assert controller.view.attachments == [image]
    controller.select.return_value = SelectorOutcome()
    await controller.handle("/attachments edit")
    assert controller.view.attachments == [image]
    await controller.handle("/attachments remove 1")
    assert not controller.view.attachments
    assert controller.view.draft == "Explain  in detail"


@pytest.mark.asyncio
async def test_attach_ambiguous_choice_does_not_consume_images(controller, tmp_path):
    from coderai.ui.shell.submission import SelectorOutcome

    for folder in ("one", "two"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "same.py").write_text(folder)
    target = tmp_path / "two" / "same.py"
    controller.view.attachments = [{"name": "pending.png"}]
    controller.select = AsyncMock(return_value=SelectorOutcome(str(target)))
    await controller.handle("/attach same.py")
    assert controller.view.draft == f'@"{target}"'
    assert controller.view.attachments == [{"name": "pending.png"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_image_history_updates_loaded_recall(tmp_path, newline):
    from coderai.ui.shell.prompt import _PasteSafeFileHistory

    history = _PasteSafeFileHistory(str(tmp_path / "history"))
    history.append_string("inspect image")
    original = [text async for text in history.load()]
    assert original == ["inspect image"]
    path = tmp_path / "history"
    path.write_bytes(path.read_bytes().replace(b"\n", newline))
    with patch("coderai.ui.shell.prompt._get_history_file", return_value=tmp_path / "history"):
        history.replace_last(str(tmp_path), "inspect image [attachments:cached]")
    assert [text async for text in history.load()] == ["inspect image [attachments:cached]"]
    reloaded = _PasteSafeFileHistory(str(tmp_path / "history"))
    assert [text async for text in reloaded.load()] == ["inspect image [attachments:cached]"]


@pytest.mark.asyncio
async def test_provider_selector_never_uses_raw_menu(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.select = AsyncMock(return_value=SelectorOutcome("chosen-model"))
    with (
        patch(
            "coderai.ui.shell.session_picker.get_models_by_provider",
            return_value={"openai": ["chosen-model"], "anthropic": ["other"]},
        ),
        patch(
            "coderai.ui.shell.session_picker.select_model_interactive",
            side_effect=AssertionError("raw input owner"),
        ),
    ):
        await controller.handle("/model openai")
    assert [r.id for r in controller.select.call_args.args[1]] == ["chosen-model", "__back__"]
    controller.mgr.set_model.assert_called_once_with("chosen-model")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,provider,model",
    [
        ("/model", "openai", "gpt-test"),
        ("/models", "anthropic", "claude-test"),
    ],
)
async def test_model_picker_opens_provider_submenus(controller, command, provider, model):
    from coderai.ui.shell.submission import SelectorOutcome

    grouped = {
        "openai": [("gpt-test", "OpenAI test model", "OpenAI")],
        "anthropic": [("claude-test", "Anthropic test model", "Anthropic")],
    }
    controller.select = AsyncMock(side_effect=[SelectorOutcome(provider), SelectorOutcome(model)])
    with patch("coderai.ui.shell.session_picker.get_models_by_provider", return_value=grouped):
        await controller.handle(command)
    calls = controller.select.call_args_list
    assert calls[0].args[0] == "Model providers"
    assert [row.id for row in calls[0].args[1]] == ["openai", "anthropic"]
    assert [row.label for row in calls[0].args[1]] == ["OpenAI (1)", "Anthropic (1)"]
    assert [row.id for row in calls[1].args[1]] == [model, "__back__"]
    assert calls[1].args[1][0].detail.startswith(grouped[provider][0][1] + "\n")
    assert "Pricing:" in calls[1].args[1][0].detail
    assert "Images:" in calls[1].args[1][0].detail
    controller.mgr.set_model.assert_called_once_with(model)


@pytest.mark.asyncio
async def test_model_submenu_back_allows_switching_provider(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.select = AsyncMock(
        side_effect=[
            SelectorOutcome("openai"),
            SelectorOutcome("__back__"),
            SelectorOutcome("anthropic"),
            SelectorOutcome("claude-test"),
        ]
    )
    with patch(
        "coderai.ui.shell.session_picker.get_models_by_provider",
        return_value={
            "openai": [("gpt-test", "", "")],
            "anthropic": [("claude-test", "", "")],
        },
    ):
        await controller.handle("/model")
    assert [call.args[0] for call in controller.select.call_args_list] == [
        "Model providers",
        "OpenAI models",
        "Model providers",
        "Anthropic models",
    ]
    controller.mgr.set_model.assert_called_once_with("claude-test")


@pytest.mark.asyncio
async def test_openrouter_model_picker_filters_by_model_maker(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    target = "openrouter/anthropic/claude-test:free"
    controller.select = AsyncMock(
        side_effect=[SelectorOutcome("anthropic"), SelectorOutcome(target)]
    )
    with patch(
        "coderai.ui.shell.session_picker.get_models_by_provider",
        return_value={
            "openai": [("gpt-test", "", "")],
            "openrouter": [
                ("openrouter/openai/gpt-test:free", "OpenAI via router", ""),
                (target, "Anthropic via router", ""),
                ("openrouter/new-maker/new-model:free", "New model maker", ""),
            ],
        },
    ):
        await controller.handle("/model openrouter")
    calls = controller.select.call_args_list
    assert calls[0].args[0] == "OpenRouter model providers"
    assert [row.label for row in calls[0].args[1]] == [
        "Anthropic (1)",
        "New Maker (1)",
        "OpenAI (1)",
        "← Back to providers",
    ]
    assert calls[1].args[0] == "OpenRouter / Anthropic models"
    assert [row.id for row in calls[1].args[1]] == [target, "__back__"]
    controller.mgr.set_model.assert_called_once_with(target)


@pytest.mark.asyncio
async def test_model_picker_cancel_returns_through_each_submenu(controller):
    from coderai.ui.shell.submission import SelectorOutcome

    controller.select = AsyncMock(
        side_effect=[
            SelectorOutcome("openrouter"),
            SelectorOutcome("liquid"),
            SelectorOutcome(),
            SelectorOutcome(),
            SelectorOutcome(),
        ]
    )
    with patch(
        "coderai.ui.shell.session_picker.get_models_by_provider",
        return_value={
            "openrouter": [("openrouter/liquid/test:free", "", "")],
        },
    ):
        await controller.handle("/model")
    assert [call.args[0] for call in controller.select.call_args_list] == [
        "Model providers",
        "OpenRouter model providers",
        "OpenRouter / LiquidAI models",
        "OpenRouter model providers",
        "Model providers",
    ]
    controller.mgr.set_model.assert_not_called()


@pytest.mark.asyncio
async def test_direct_model_id_skips_submenus(controller):
    controller.select = AsyncMock()
    with patch(
        "coderai.ui.shell.session_picker.get_models_by_provider",
        return_value={"openai": ["gpt-test"]},
    ):
        await controller.handle("/model openrouter/anthropic/custom-model")
    controller.select.assert_not_called()
    controller.mgr.set_model.assert_called_once_with("openrouter/anthropic/custom-model")


@pytest.mark.asyncio
async def test_accessible_picker_uses_provider_and_model_menus(controller, capsys):
    controller.preferences.accessible = True
    controller.prompt.prompt_async = AsyncMock(side_effect=["2", "1"])
    with patch(
        "coderai.ui.shell.session_picker.get_models_by_provider",
        return_value={
            "openai": [("gpt-test", "OpenAI test model", "")],
            "anthropic": [("claude-test", "Anthropic test model", "")],
        },
    ):
        await controller.handle("/model")
    displayed = capsys.readouterr().out
    assert "Model providers" in displayed
    assert "1. OpenAI (1)" in displayed and "2. Anthropic (1)" in displayed
    assert "Anthropic models" in displayed and "1. claude-test" in displayed
    assert "gpt-test" not in displayed
    controller.mgr.set_model.assert_called_once_with("claude-test")


def test_catalog_help_includes_every_command_and_unicode_labels_fit(capsys):
    from coderai.ui.shell.slash import COMMAND_CATALOG, HELP_GROUPS, render_help
    from coderai.ui.shell.interaction import fit_label
    from prompt_toolkit.utils import get_cwidth

    entries = {
        name.split(",")[0].removeprefix("/") for _, group in HELP_GROUPS for name, _, _ in group
    }
    assert set(COMMAND_CATALOG) <= entries
    render_help("queue")
    output = capsys.readouterr().out
    assert "move <from> <to>" in output and "Indices start at 1" in output
    for width in (1, 2, 3, 40, 60, 80, 120, 200):
        label = fit_label("长 " * 100 + "\nother line", width)
        assert "\n" not in label and sum(get_cwidth(char) for char in label) <= width


def test_output_inspector_reads_full_owned_spill_and_rejects_other_session(tmp_path):
    import json
    from coderai.spill import save_text
    from coderai.ui.shell.output import message_output

    mgr = SimpleNamespace(
        project_root=str(tmp_path), session_store=SimpleNamespace(project_dir=tmp_path / "sessions")
    )
    root = tmp_path / "sessions" / "tool-results"
    full = "\n".join(f"line {i}" for i in range(1000))
    reference = save_text(session_id="s1", suggested_name="output", content=full, root=root)
    message = SessionMessage(
        "m1",
        "s1",
        "tool",
        json.dumps(
            {
                "name": "bash",
                "output": "truncated preview",
                "metadata": {"spill": reference.to_dict()},
            }
        ),
    )
    expanded = message_output(message, mgr)
    assert full in expanded and "Result metadata:" in expanded
    message.session_id = "other"
    inaccessible = message_output(message, mgr)
    assert "line 999" not in inaccessible and "another session" in inaccessible
    message.session_id = "s1"
    from pathlib import Path

    Path(reference.locator).unlink()
    assert "unavailable" in message_output(message, mgr)


def test_semantic_console_theme_and_static_accessibility_preferences(tmp_path, monkeypatch):
    from coderai.ui.shell.console import _CoderAIConsole
    from coderai.ui.theme import get_semantic_rich_styles, set_active_theme
    from coderai.ui.shell import session_picker

    monkeypatch.delenv("NO_COLOR", raising=False)
    for theme in ("dark", "light"):
        set_active_theme(theme)
        output = io.StringIO()
        console = _CoderAIConsole(file=output, force_terminal=True, color_system="truecolor")
        console.print("[bold green]OK[/] [bold red]FAILED[/]")
        success = get_semantic_rich_styles()["success"].lstrip("#")
        rgb = tuple(int(success[i : i + 2], 16) for i in (0, 2, 4))
        assert f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}" in output.getvalue()
    set_active_theme("dark")
    with (
        patch("coderai.config.read_settings", return_value={"display": {"accessible": True}}),
        patch.object(session_picker.sys.stdin, "isatty", return_value=True),
        patch.object(session_picker, "_read_single_key", side_effect=AssertionError("raw menu")),
        patch("builtins.input", return_value="1"),
    ):
        assert session_picker.select_with_arrows(None, [("first", "First", "")]) == 0


@pytest.mark.asyncio
async def test_accessible_browser_searches_all_records_and_cancel_is_explicit():
    prompt = SimpleNamespace(prompt_async=AsyncMock(side_effect=["/search needle", "1"]))
    rows = [BrowserRow(str(i), "needle" if i == 80 else f"row {i}") for i in range(100)]
    assert (await choose(prompt, "Accessible search", rows, accessible=True)).value == "80"
    prompt.prompt_async.side_effect = EOFError
    assert (await choose(prompt, "Accessible cancel", rows, accessible=True)).cancelled


@pytest.mark.parametrize("width", [40, 60, 80, 120, 200])
def test_critical_toolbar_fits_cells(width):
    from prompt_toolkit.utils import get_cwidth

    tokens = get_bottom_toolbar_tokens(
        "/very/long/workspace",
        True,
        active_model="model" * 100,
        yolo=True,
        width=width,
        state="approval",
        extra_info="queued:2",
    )
    text = "".join(value for _, value in tokens)
    assert sum(get_cwidth(c) for c in text) <= width
    assert "plan:ON" in text or "plan: ON" in text
    assert "YOLO" in text and "approval" in text


def test_cancelling_toolbar_preserves_both_permission_flags_and_queue():
    tokens = get_bottom_toolbar_tokens(
        "/workspace",
        True,
        yolo=True,
        afk=True,
        width=40,
        state="cancelling",
        execution_mode="shell",
        extra_info="queued:2",
    )
    text = "".join(value for _, value in tokens)
    assert len(text) <= 40
    assert all(value in text for value in ("shell", "cancelling", "plan:ON", "YOLO+AFK", "q:2"))


@pytest.mark.asyncio
async def test_runtime_mode_and_plan_status_distinguish_the_next_turn(controller):
    controller.view.session_id = "s1"
    controller.mgr.get_session.return_value.plan_mode = False
    controller.submission = PromptSubmission("sleep 1", "sleep 1", action="shell")
    controller.active = asyncio.create_task(asyncio.sleep(1))
    controller.toggle_plan(True)
    stats = controller.stats()
    assert stats["execution_mode"] == "shell"
    assert stats["plan_mode"] is False
    assert stats["pending"] == "next plan:ON"
    controller.stop()
    await controller.settle()
    assert controller.stats()["plan_mode"] is True


def test_context_and_mixed_model_total_use_runtime_inputs(controller):
    entry = SessionEntry(
        "s1",
        usage={"prompt_tokens": 9999999},
        usage_per_model={
            "gpt-6-sol": {"prompt_tokens": 1000000},
            "gpt-6-astra": {"completion_tokens": 1000000},
        },
    )
    assert context_limit(controller.mgr) == 32000
    assert session_cost(entry, "gpt-6-luna") == 52.0
    entry.usage_per_model["unknown-local"] = {"prompt_tokens": 100}
    assert session_cost(entry, "gpt-6-luna") is None


def test_all_records_search_and_literal_render_commit_once(controller):
    rows = [BrowserRow(str(i), f"session {i}", "needle" if i == 80 else "") for i in range(100)]
    assert filter_rows(rows, "needle")[0].id == "80"
    assert filter_rows(rows, "nonexistent") == []
    controller.presentation.switch("s1")
    msg = SessionMessage("m", "s1", "assistant", "Hello [/white]")
    controller.presentation.chunk(msg.content)
    controller.presentation.message(msg)
    controller.presentation.message(msg)
    controller.presentation.complete("ready")
    assert controller.console.file.getvalue().count("Hello") == 1


def test_completion_summary_recognizes_checks_without_filename_false_positives():
    from coderai.ui.shell.presentation import _is_check_command

    for command in (
        ".venv/bin/python -m pytest tests/test_ui.py",
        "uv run ruff check .",
        "cd src && npm run test",
        "make check",
        "cargo clippy",
    ):
        assert _is_check_command(command)
    for command in (
        "cat latest.txt",
        "echo pytest",
        "git diff tests/",
        "cat build.log",
        "printf 'check'",
    ):
        assert not _is_check_command(command)


@pytest.mark.asyncio
async def test_async_browser_no_match_cancels_without_blocking_heartbeat():
    with create_pipe_input() as pipe:
        prompt = SimpleNamespace(
            session=SimpleNamespace(
                app=SimpleNamespace(input=pipe, output=DummyOutput(), color_depth=None)
            )
        )
        task = asyncio.create_task(choose(prompt, "Sessions", [BrowserRow("first", "only record")]))
        await asyncio.sleep(0.05)
        pipe.send_text("no-such-result\r")
        await asyncio.sleep(0.05)
        assert not task.done()
        # The browser leaves the event loop alive, and Escape cancels.
        pipe.send_bytes(b"\x03")
        assert (await asyncio.wait_for(task, 2)).cancelled


@pytest.mark.asyncio
async def test_inspector_contains_full_large_output():
    with create_pipe_input() as pipe:
        prompt = SimpleNamespace(
            session=SimpleNamespace(app=SimpleNamespace(input=pipe, output=DummyOutput()))
        )
        text = "\n".join(f"line {i}" for i in range(1000))
        task = asyncio.create_task(inspect_output(prompt, "Complete output", text))
        await asyncio.sleep(0.05)
        pipe.send_bytes(b"\x03")
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_ambiguous_file_requires_explicit_choice(controller, tmp_path):
    from coderai.ui.shell.submission import SelectorOutcome

    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "same.py").write_text("one content")
    chosen = tmp_path / "two" / "same.py"
    chosen.write_text("two content")
    controller.select = AsyncMock(return_value=SelectorOutcome(str(chosen)))
    submission = await controller.prepare_async("inspect @same.py")
    assert "two content" in submission.resolved_text
    assert "one content" not in submission.resolved_text
    controller.select.return_value = SelectorOutcome()
    assert await controller.prepare_async("inspect @same.py") is None


def test_attachment_history_roundtrip_and_stale_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from coderai.ui.shell.submission_history import SubmissionHistory
    from coderai.ui.shell.submission import prepare_submission

    history = SubmissionHistory(str(tmp_path))
    attachments = [
        {"type": "image_url", "image_url": {"url": "data:fixture"}, "name": "spaces image.png"}
    ]
    text = history.save("inspect this", attachments)
    recalled = prepare_submission(text, str(tmp_path))
    assert recalled.attachments == attachments
    assert recalled.resolved_text == "inspect this"
    for path in history.folder.glob("*.json"):
        path.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        prepare_submission(text, str(tmp_path))


def test_completion_quotes_spaces_and_additional_roots(tmp_path):
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent
    from coderai.ui.shell.prompt_completers import FileMentionCompleter

    extra = tmp_path / "extra"
    extra.mkdir()
    target = extra / "unicode λ file.py"
    target.write_text("content")
    root = tmp_path / "workspace"
    root.mkdir()
    completer = FileMentionCompleter(str(root), lambda: [str(extra)])
    choices = list(completer.get_completions(Document('@"unicode λ'), CompleteEvent()))
    assert any(choice.text == f'"{target}"' for choice in choices)
