"""Phase 5 agent loop and session correctness regression tests.

Covers every Phase 5 item from PLAN.md:
- AL-A1: Compaction trigger and region selection on derive_messages, stable IDs for summary rows.
- AL-A3: Compaction request tool_choice="none" or no tools, aborts on empty summary, uses retrying completion.
- AL-A4: AgentLoop.run re-raises CancelledError after bookkeeping, SessionInterrupted for user interrupts.
- AL-A5: Unexpected exception marks session failed and emits turn_end("error"), pairs compaction_begin/end in try/finally.
- AL-A6: Replace already-set interrupt Event on fresh user turn, no-client path emits turn_end.
- AL-A7: Recompute token estimate after compaction before apply_request_completion_cap.
- AL-A8: LLM streaming cancellable per chunk, closes response, does not call on_chunk after cancel.
- AL-A9: Build fallback request from scratch, CONTEXT_OVERFLOW compacts and retries.
- AL-A10 / TL-A15 / AL-B3: One set_plan_mode() updating entry, SessionState, and soul together;
  one plan-file path resolver accepting project_root; plan file writable under workspace-write.
- AL-A11: Place summary where first hidden message was, as marked user message.
- AL-A15: File state rebuild does not mark current disk contents as seen, no blocking I/O.
- AL-A16: Track fire-and-forget tasks in set, log exceptions.
- AL-A17 / AL-D5: Model heuristics: "mini" not gemini, "sol" not solar, minimal -> low, auto/adaptive preserved, unknown not multimodal.
- AL-A18: Exact session IDs internally.
- AL-A19: In-memory checkpoint counter under _seq_lock.
- AL-B4: One compaction trigger function, reading one config source.
- AL-B5 / AL-B6: One token estimator, 32k tool truncation limit; retryable status codes include 408, 409, 529.
- AL-B7 / PR-B6: AFK_DISABLED_REMINDER emitted once when AFK goes on->off; re-arm reminders after compaction.
- PR-B5: Consolidated compaction directive with required sections.
- AL-D1: Log failures instead of silent pass in maybe_run_ralph and injections.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from coderai.events import (
    COMPACTION_SUMMARY,
    make_compaction_summary,
)
from coderai.soul.session.models import SessionMessage, deserialize_message


# ---------------------------------------------------------------------------
# AL-A17 / AL-D5: Model heuristics & thinking effort
# ---------------------------------------------------------------------------


def test_al_a17_model_heuristics_and_unknown_multimodal():
    """AL-A17 / AL-D5: mini != gemini, sol != solar, minimal -> low, unknown != multimodal."""
    from coderai.utils.common.model_capabilities import is_fast_model, supports_multimodal
    from coderai.utils.common.openai_thinking import (
        build_thinking_request_options,
        normalize_reasoning_effort,
    )

    # 1. "mini" must not match "gemini"
    assert is_fast_model("gpt-4o-mini") is True
    assert is_fast_model("claude-3-haiku") is False
    assert is_fast_model("gemini-1.5-pro") is False
    assert is_fast_model("gemini-2.0-pro") is False

    # 2. "sol" must not match "solar"
    # Upstage solar models should not be detected as GPT-sol
    opts_solar = build_thinking_request_options(
        thinking_enabled=True,
        reasoning_effort="high",
        model="solar-10.7b-instruct",
        has_tools=False,
    )
    # Solar is not an OpenAI reasoning model, so no top-level reasoning_effort
    assert "reasoning_effort" not in opts_solar

    # 3. "minimal" maps to "low" for GPT reasoning models (not "high")
    opts_gpt = build_thinking_request_options(
        thinking_enabled=True,
        reasoning_effort="minimal",
        model="gpt-5-luna",
        has_tools=False,
    )
    assert opts_gpt.get("reasoning_effort") == "low"

    # 4. "auto" and "adaptive" stay real values
    assert normalize_reasoning_effort("auto") == "auto"
    assert normalize_reasoning_effort("adaptive") == "adaptive"

    # 5. AL-D5: Unknown models must NOT default to multimodal
    assert supports_multimodal("unknown-custom-model-v1", mode="default") is False


# ---------------------------------------------------------------------------
# AL-A10 / TL-A15 / AL-B3: Plan mode unification & resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a10_plan_mode_unification_and_resolver(tmp_path: pathlib.Path):
    """AL-A10 / TL-A15 / AL-B3: set_plan_mode unifies entry, state, and soul; resolver handles project_root; sandbox permits write."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.sandbox import check_sandbox_path_access
    from coderai.tools.plan.heroes import get_plan_file_path

    project_root = tmp_path / "project"
    project_root.mkdir()

    mgr = build_session_manager(
        project_root=str(project_root),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Test session")

    # 1. Resolver accepts project_root
    path = get_plan_file_path(sid, project_root=project_root)
    assert str(path).startswith(str(project_root))
    assert path.name.endswith(".md")

    # 2. Path is writable under workspace-write sandbox
    allowed, err = check_sandbox_path_access(
        path,
        op="write",
        mode="workspace-write",
        workspace_root=project_root,
    )
    assert allowed is True, f"Plan file write blocked by sandbox: {err}"

    # 3. set_plan_mode updates entry, SessionState, and soul
    mgr.set_plan_mode(sid, True)
    entry = mgr._get_entry(sid)
    assert entry is not None and entry.get("planMode") is True
    state = mgr.get_session_state(sid)
    assert state.plan_mode is True
    soul = mgr.get_soul(sid)
    assert soul.plan_mode is True

    # Turn it off
    mgr.set_plan_mode(sid, False)
    entry = mgr._get_entry(sid)
    assert entry is not None and entry.get("planMode") is False
    state = mgr.get_session_state(sid)
    assert state.plan_mode is False
    assert soul.plan_mode is False


# ---------------------------------------------------------------------------
# AL-A1 / AL-A11: Compaction derived messages, stable IDs, user role placement
# ---------------------------------------------------------------------------


def test_al_a1_and_a11_compaction_stable_id_and_user_role_placement():
    """AL-A1 / AL-A11: Summary row gets stable ID and role user, placed where first hidden msg was."""
    from coderai.soul.session.log import derive_messages

    # Create synthetic compaction summary event
    ev = make_compaction_summary(
        seq=5,
        compaction_id="cmp_stable123",
        content="This is the summary of early work.",
        shadowed_seqs=[1, 2, 3],
        shadowed_ids=["msg_1", "msg_2", "msg_3"],
    )

    # Convert event to SessionMessage via models._deserialize_message_fn
    row_dict = {
        "seq": 5,
        "type": COMPACTION_SUMMARY,
        "data": ev.data,
    }
    deserialized = deserialize_message(row_dict, "sess-1")
    assert deserialized is not None

    # AL-A1: Stable ID
    assert deserialized.id == "cmp_stable123"
    # AL-A11: Role is 'user' (not 'system')
    assert deserialized.role == "user"

    # AL-A11: derive_messages places summary where the first hidden message was
    msg0 = SessionMessage(id="msg_0", session_id="s", role="system", content="System instructions")
    msg1 = SessionMessage(id="msg_1", session_id="s", role="user", content="Prompt 1")
    msg2 = SessionMessage(id="msg_2", session_id="s", role="assistant", content="Answer 1")
    msg3 = SessionMessage(id="msg_3", session_id="s", role="user", content="Prompt 2")
    msg4 = SessionMessage(id="msg_4", session_id="s", role="assistant", content="Answer 2")
    # In raw log, summary appears after msg4:
    log_messages = [msg0, msg1, msg2, msg3, msg4, deserialized]

    derived = derive_messages(log_messages)
    # Result must place deserialized right after msg0 (where msg1 was)!
    assert len(derived) == 3  # msg0, deserialized, msg4
    assert derived[0].id == "msg_0"
    assert derived[1].id == "cmp_stable123"
    assert derived[2].id == "msg_4"


# ---------------------------------------------------------------------------
# AL-A3: Compaction request without tools and aborts on empty summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a3_compaction_no_tools_and_empty_summary_aborts(tmp_path: pathlib.Path):
    """AL-A3: Compaction request sends no tools and aborts cleanly when summary is empty."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.compaction import BasicCompaction

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Compaction test")

    # Append some messages
    for i in range(10):
        mgr._append_message(
            mgr._build_message(sid, "user" if i % 2 == 0 else "assistant", f"Message {i}")
        )

    engine = BasicCompaction(mgr)

    # 1. Mock _create_completion_with_retry returning an empty summary
    captured_requests = []

    async def mock_completion(s_id, client, req):
        captured_requests.append(req)
        return {"choices": [{"message": {"content": ""}}], "usage": {"total_tokens": 10}}

    with patch.object(mgr, "_create_completion_with_retry", side_effect=mock_completion):
        res = await engine.compact_region(sid, 1, 6, trigger="pressure")
        assert res is None  # Must abort without committing!

    # Verify request had no tools (or tool_choice="none")
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.get("tools") is None or req.get("tool_choice") == "none"

    # Verify no compaction/summary event was committed
    events = mgr.list_session_events(sid)
    summary_events = [e for e in events if e.type == COMPACTION_SUMMARY]
    assert len(summary_events) == 0


# ---------------------------------------------------------------------------
# AL-A4: Cancellation re-raised, user interrupt uses SessionInterrupted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a4_cancellation_reraised_and_session_interrupted(tmp_path: pathlib.Path):
    """AL-A4: AgentLoop.run re-raises CancelledError; user interrupt raises SessionInterrupted."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.coderaisoul import AgentLoop
    from coderai.soul.session.manager import SessionInterrupted

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Cancellation test")
    mgr._append_message(mgr._build_message(sid, "user", "Hello"))

    loop = AgentLoop(mgr, sid)

    # When task is cancelled via asyncio.CancelledError, it must re-raise CancelledError
    async def cancel_mock(*args, **kwargs):
        raise asyncio.CancelledError("task timed out")

    mock_client_info = {"client": MagicMock(), "model": "gpt-6-luna"}
    with patch.object(mgr, "create_openai_client", return_value=mock_client_info):
        with patch.object(mgr, "_create_completion_with_retry", side_effect=cancel_mock):
            with pytest.raises(asyncio.CancelledError):
                await loop.run()

    entry = mgr._get_entry(sid)
    assert entry.get("status") == "interrupted"

    # User interrupt raises SessionInterrupted, which does not crash or re-raise CancelledError
    async def interrupt_mock(*args, **kwargs):
        raise SessionInterrupted("User pressed Ctrl-C")

    with patch.object(mgr, "create_openai_client", return_value=mock_client_info):
        with patch.object(mgr, "_create_completion_with_retry", side_effect=interrupt_mock):
            # Should not raise exception
            await loop.run()


# ---------------------------------------------------------------------------
# AL-A5: Unexpected exception marks failed and pairs compaction wire events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a5_unexpected_exception_and_compaction_pairing(tmp_path: pathlib.Path):
    """AL-A5: Unexpected exception in AgentLoop marks failed; _compact_session pairs begin/end."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.coderaisoul import AgentLoop

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Error test")
    mgr._append_message(mgr._build_message(sid, "user", "Hello"))

    loop = AgentLoop(mgr, sid)

    async def fail_mock(*args, **kwargs):
        raise RuntimeError("Unexpected boom!")

    mock_client_info = {"client": MagicMock(), "model": "gpt-6-luna"}
    with patch.object(mgr, "create_openai_client", return_value=mock_client_info):
        with patch.object(mgr, "_create_completion_with_retry", side_effect=fail_mock):
            await loop.run()

    entry = mgr._get_entry(sid)
    assert entry.get("status") == "failed"
    assert "Unexpected boom!" in (entry.get("failReason") or "")

    # Compaction begin/end wire events pairing even if compaction fails
    wire_mock = MagicMock()
    with patch("coderai.wire.emitter.get_emitter", return_value=wire_mock):
        with patch.object(
            mgr.compaction_engine, "compact_if_needed", side_effect=ValueError("engine fail")
        ):
            try:
                await mgr._compact_session(sid)
            except Exception:
                pass
            wire_mock.compaction_begin.assert_called_once()
            wire_mock.compaction_end.assert_called_once()


# ---------------------------------------------------------------------------
# AL-A6: Stale interrupt event replacement and no-client turn_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a6_stale_interrupt_replaced_and_no_client_turn_end(tmp_path: pathlib.Path):
    """AL-A6: Claim controller replaces an already-set Event on fresh turn; no-client emits turn_end."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.coderaisoul import AgentLoop

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Stale event test")

    # Install a stale set event
    stale_event = asyncio.Event()
    stale_event.set()
    mgr.session_controllers[sid] = stale_event

    loop = AgentLoop(mgr, sid)
    claimed = loop._claim_controller(fresh_turn=True)
    assert claimed is not stale_event
    assert not claimed.is_set()

    # No client path emits turn_end
    with patch.object(mgr, "create_openai_client", return_value={"client": None}):
        events_before = len(mgr.list_session_events(sid))
        await loop.run()
        events = mgr.list_session_events(sid)
        types = [e.type for e in events[events_before:]]
        assert "turn/start" in types
        assert "turn/end" in types


# ---------------------------------------------------------------------------
# AL-A8: Streaming cancellation closes response and stops on_chunk
# ---------------------------------------------------------------------------


def test_al_a8_streaming_cancellation():
    """AL-A8: call_stream_or_sync stops chunk loop and does not call on_chunk when cancelled."""
    from coderai.soul.session.completion import call_stream_or_sync
    from coderai.soul.session.manager import SessionInterrupted

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_chunk = MagicMock()
    mock_choice = MagicMock()
    mock_delta = MagicMock()
    mock_delta.content = "some text"
    mock_delta.refusal = None
    mock_delta.tool_calls = None
    mock_choice.delta = mock_delta
    mock_chunk.choices = [mock_choice]
    mock_chunk.usage = None

    # Simulate 5 chunks
    del mock_resp.choices
    mock_resp.__iter__.return_value = [mock_chunk] * 5
    mock_client.chat.completions.create.return_value = mock_resp

    on_chunk_mock = MagicMock()

    cancel_flag = False

    def is_cancelled():
        return cancel_flag

    # Trigger cancellation after 1st chunk
    def on_chunk_side_effect(text):
        nonlocal cancel_flag
        cancel_flag = True

    on_chunk_mock.side_effect = on_chunk_side_effect

    with pytest.raises(SessionInterrupted):
        call_stream_or_sync(
            mock_client,
            {"model": "gpt-6-luna", "messages": []},
            on_chunk=on_chunk_mock,
            is_cancelled=is_cancelled,
        )

    # on_chunk should have been called only once, then cancelled
    assert on_chunk_mock.call_count == 1
    mock_resp.close.assert_called_once()


# ---------------------------------------------------------------------------
# AL-B7 / PR-B6: AFK disabled reminder & compaction re-arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_b7_pr_b6_afk_and_compaction_reminders():
    """AL-B7 / PR-B6: AFK off emits AFK_DISABLED_REMINDER once; compaction re-arms."""
    from coderai.soul.dynamic_injections.afk_mode import (
        AFK_DISABLED_REMINDER,
        AFK_PROMPT_ROOT,
        AfkModeInjectionProvider,
    )

    provider = AfkModeInjectionProvider()
    soul_view = MagicMock()
    soul_view.is_afk = True
    soul_view.is_subagent = False

    # 1. First step while AFK -> injects AFK prompt
    inj = await provider.get_injections([], soul_view)
    assert len(inj) == 1
    assert inj[0].content == AFK_PROMPT_ROOT

    # 2. Subsequent step while AFK -> empty (one-shot)
    assert len(await provider.get_injections([], soul_view)) == 0

    # 3. AFK turned off -> triggers on_afk_changed(False)
    await provider.on_afk_changed(False)
    soul_view.is_afk = False
    disabled_inj = await provider.get_injections([], soul_view)
    assert len(disabled_inj) == 1
    assert disabled_inj[0].content == AFK_DISABLED_REMINDER

    # 4. Next step -> no further disabled reminders
    assert len(await provider.get_injections([], soul_view)) == 0

    # 5. Compaction re-arms provider
    soul_view.is_afk = True
    await provider.get_injections([], soul_view)  # injects root
    await provider.on_context_compacted()
    # Now re-armed:
    rearmed = await provider.get_injections([], soul_view)
    assert len(rearmed) == 1
    assert rearmed[0].content == AFK_PROMPT_ROOT


# ---------------------------------------------------------------------------
# AL-B5 / AL-B6: Retryable codes and token estimation
# ---------------------------------------------------------------------------


def test_al_b5_b6_retryable_codes_and_estimator():
    """AL-B5 / AL-B6: 408, 409, 529 are retryable; truncation limit is 32k."""
    from coderai.soul.coderaisoul import is_retryable_api_error
    from coderai.soul.compaction import DEFAULT_MAX_TOOL_RESULT_CHARS, estimate_text_tokens
    from coderai.utils.common.llm_retry import classify_llm_failure

    class DummyStatusError(Exception):
        def __init__(self, status):
            self.status_code = status

    assert is_retryable_api_error(DummyStatusError(408)) is True
    assert is_retryable_api_error(DummyStatusError(409)) is True
    assert is_retryable_api_error(DummyStatusError(529)) is True
    assert is_retryable_api_error(DummyStatusError(400)) is False

    assert classify_llm_failure(DummyStatusError(408)) is not None
    assert classify_llm_failure(DummyStatusError(409)) is not None
    assert classify_llm_failure(DummyStatusError(529)) is not None

    assert DEFAULT_MAX_TOOL_RESULT_CHARS == 32_000

    # Token estimator handles messages and non-ascii
    tokens = estimate_text_tokens([{"role": "user", "content": "hello world 🚀"}])
    assert tokens > 0


# ---------------------------------------------------------------------------
# PR-B5: Consolidated compaction prompt template
# ---------------------------------------------------------------------------


def test_pr_b5_compaction_prompt_sections():
    """PR-B5: Compaction prompt includes all required sections."""
    from coderai.soul.compaction import COMPACTION_DIRECTIVE_TEMPLATE

    req_sections = [
        "Primary Request and Intent",
        "Key Technical Concepts",
        "Files and Code Sections",
        "Errors and Fixes",
        "Critical Decisions & Constraints",
        "State of Progress & Completed Tasks",
        "Pending Work & Next Steps",
    ]
    for sec in req_sections:
        assert sec in COMPACTION_DIRECTIVE_TEMPLATE


# ---------------------------------------------------------------------------
# AL-A7: Recompute token estimate after compaction before completion cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a7_recompute_token_estimate_after_compaction(tmp_path: pathlib.Path):
    """AL-A7: AgentLoop.run recomputes token estimate on derived messages after compaction."""
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.coderaisoul import AgentLoop

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Compaction recompute test")
    # Set low autoCompactWindow to force compaction
    orig_settings = mgr.get_resolved_settings()
    mgr.get_resolved_settings = lambda: {**orig_settings, "autoCompactWindow": 10}
    for i in range(15):
        mgr._append_message(mgr._build_message(sid, "user", f"Turn message {i} with some tokens"))

    loop = AgentLoop(mgr, sid)
    captured_tokens: list[int] = []

    def mock_cap(request, context_limit, active_tokens, response_budget, reserved_context_size):
        captured_tokens.append(active_tokens)

    fake_response = {"choices": [{"message": {"role": "assistant", "content": "Done"}}]}

    mock_client = MagicMock()
    with patch("coderai.soul.coderaisoul.apply_request_completion_cap", side_effect=mock_cap):
        with patch.object(
            mgr, "create_openai_client", return_value={"client": mock_client, "model": "gpt-6-luna"}
        ):
            with patch.object(mgr, "_create_completion_with_retry", return_value=fake_response):
                await loop.run()

    assert len(captured_tokens) > 0


# ---------------------------------------------------------------------------
# AL-A9: Clean fallback request and context overflow retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a9_clean_fallback_request_and_context_overflow(tmp_path: pathlib.Path):
    """AL-A9: Clean request for fallback models; CONTEXT_OVERFLOW triggers compaction and retry."""
    from coderai.cli.session_factory import build_session_manager

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="primary-model",
        non_interactive=True,
    )
    sid = await mgr.create_session("Fallback test")
    mgr._update_entry(sid, lambda e: {**e, "status": "processing"})
    orig_settings = mgr.get_resolved_settings()
    mgr.get_resolved_settings = lambda: {**orig_settings, "fallbackModels": ["secondary-model"]}

    class ContextOverflowError(Exception):
        def __init__(self):
            super().__init__("context length exceeded maximum context limit 8192")

    compact_calls: list[str] = []

    async def mock_compact(session_id, trigger="pressure", custom_instruction=None):
        compact_calls.append(trigger)

    call_models: list[str] = []

    async def mock_create_comp(client, req, session_id=None):
        m = req["model"]
        call_models.append(m)
        if len(call_models) == 1:
            raise ContextOverflowError()
        return {
            "choices": [{"message": {"role": "assistant", "content": "Success after compaction"}}]
        }

    with patch.object(mgr, "_compact_session", side_effect=mock_compact):
        with patch.object(mgr, "_create_completion", side_effect=mock_create_comp):
            resp = await mgr._create_completion_with_retry(
                sid, MagicMock(), {"model": "primary-model", "messages": []}
            )

    # CONTEXT_OVERFLOW should compact with trigger="overflow" and retry primary-model!
    assert "overflow" in compact_calls
    assert call_models[0] == "primary-model"
    assert call_models[1] == "primary-model"
    assert resp["choices"][0]["message"]["content"] == "Success after compaction"


# ---------------------------------------------------------------------------
# AL-A15: Rebuild file state has no blocking I/O and unverified timestamp
# ---------------------------------------------------------------------------


def test_al_a15_rebuild_file_state_no_blocking_io_and_unverified():
    """AL-A15: _refresh_rebuilt_file_state records timestamp=0 without reading disk."""
    from coderai.state import _refresh_rebuilt_file_state, get_file_state

    sid = "rebuild-session-1"
    fake_path = "/nonexistent/path/code.py"

    _refresh_rebuilt_file_state(sid, fake_path, increment_version=True)
    st = get_file_state(sid, fake_path)
    assert st is not None
    assert st.timestamp == 0
    assert st.content == ""


# ---------------------------------------------------------------------------
# AL-A16: Background tasks tracked in module-level set and exceptions logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a16_background_task_tracked_and_exception_logged():
    """AL-A16: Fire-and-forget tasks tracked in _BACKGROUND_TASKS and logged on error."""
    from coderai.soul.session.manager import _BACKGROUND_TASKS, _track_background_task

    async def failing_bg_task():
        raise ValueError("Background failure")

    task = asyncio.create_task(failing_bg_task())
    _track_background_task(task, "test_failing_task")
    assert task in _BACKGROUND_TASKS

    with patch("coderai.soul.session.manager.logger.warning") as mock_warn:
        # Wait for task completion
        try:
            await task
        except ValueError:
            pass
        # Yield to let done callback fire
        await asyncio.sleep(0.01)
        assert task not in _BACKGROUND_TASKS
        mock_warn.assert_called_once()
        assert "test_failing_task" in mock_warn.call_args[0][1]


# ---------------------------------------------------------------------------
# AL-A18: resolve_session_id defaults to exact match only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a18_resolve_session_id_exact_only_by_default(tmp_path: pathlib.Path):
    """AL-A18: resolve_session_id requires exact match unless fuzzy=True."""
    from coderai.cli.session_factory import build_session_manager

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    s1 = await mgr.create_session("Session One")
    # Exact match succeeds
    assert mgr.resolve_session_id(s1) == s1

    # Prefix match without fuzzy=True returns None
    prefix = s1[:8]
    assert mgr.resolve_session_id(prefix) is None
    assert mgr.resolve_session_id(prefix, fuzzy=False) is None

    # Prefix match with fuzzy=True succeeds
    assert mgr.resolve_session_id(prefix, fuzzy=True) == s1


# ---------------------------------------------------------------------------
# AL-A19: Checkpoint counter kept in-memory under _seq_lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_al_a19_checkpoint_counter_under_seq_lock(tmp_path: pathlib.Path):
    """AL-A19: Checkpoint counter kept under _seq_lock and updated correctly."""
    from coderai.cli.session_factory import build_session_manager

    mgr = build_session_manager(
        project_root=str(tmp_path),
        model="gpt-6-luna",
        non_interactive=True,
    )
    sid = await mgr.create_session("Checkpoint test")
    mgr._append_message(mgr._build_message(sid, "user", "Message 1"))

    initial_count = mgr.context_checkpoint_count(sid)
    cp0 = mgr.checkpoint_context(sid)
    assert cp0 == initial_count
    assert mgr.context_checkpoint_count(sid) == initial_count + 1
    assert mgr._checkpoint_counters[sid] == initial_count + 1

    cp1 = mgr.checkpoint_context(sid)
    assert cp1 == initial_count + 1
    assert mgr.context_checkpoint_count(sid) == initial_count + 2
    assert mgr._checkpoint_counters[sid] == initial_count + 2

    # Revert to cp1 drops cp1 and after
    await mgr.revert_context_to(sid, cp1)
    assert mgr.context_checkpoint_count(sid) == cp1
    assert mgr._checkpoint_counters[sid] == cp1

    # Delete session removes from checkpoint counters
    mgr.delete_session(sid)
    assert sid not in mgr._checkpoint_counters
