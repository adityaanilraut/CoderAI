"""Activation and tool-iteration controller for one agent turn.

Turn/Step lifecycle::

    turn/start → step/start → derive_request → LLM call → response →
    [tool/call → tool/result]* → step/end → [turn-stopping check] → turn/end

Each turn may contain multiple steps (one LLM call + tool execution per step).
A turn ends when the model produces no tool calls (natural completion) or
when the model is interrupted/cancelled.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Any, TYPE_CHECKING

from coderai.utils.logging import log_api_error
from coderai.utils.common.llm_error import describe_llm_error
from coderai.utils.common.model_capabilities import (
    is_fast_model,
    resolve_adaptive_reasoning_effort,
)
from coderai.utils.common.openai_thinking import build_thinking_request_options
from coderai.utils.common.usage import extract_usage_dict
from coderai.soul.compaction import evaluate_compaction_trigger
from coderai.events import (
    SessionEvent,
    make_turn_start,
    make_turn_end,
    make_step_start,
    make_step_end,
)
from coderai.soul.approval import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    compute_tool_call_permissions,
    resolve_snippet_file_path,
)
from coderai.prompt import (
    calculate_context_budget,
    format_tool_definitions,
    get_tools,
)
from coderai.skill import get_skill_read_exempt_paths
from coderai.state import rebuild_session_state_from_history

if TYPE_CHECKING:
    from coderai.soul.session.manager import SessionManager


class AgentLoop:
    """Own a single activation while delegating stateful operations to its manager."""

    def __init__(self, manager: SessionManager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id
        # Resume from the persisted log so a new activation never restarts
        # turn/step numbering (and never duplicates numbers concurrently —
        # increments go through the manager lock via next_turn/next_step).
        try:
            self._turn = manager._current_turn(session_id)
            self._step = manager._current_step(session_id)
        except Exception:
            self._turn = 0
            self._step = 0
        self._controller: asyncio.Event | None = None

    def _claim_controller(self) -> asyncio.Event:
        """Return this activation's controller without clobbering a live one."""
        existing = self.manager.session_controllers.get(self.session_id)
        if existing is not None:
            self._controller = existing
            return existing
        controller = asyncio.Event()
        self.manager.session_controllers[self.session_id] = controller
        self._controller = controller
        return controller

    def _release_controller(self) -> None:
        """Remove the controller only if it is still this activation's own."""
        if self._controller is None:
            return
        try:
            if self.manager.session_controllers.get(self.session_id) is self._controller:
                self.manager.session_controllers.pop(self.session_id, None)
        except Exception:
            pass
        finally:
            self._controller = None

    def _next_seq(self) -> int:
        return self.manager._next_seq(self.session_id)

    def _emit(self, event: SessionEvent) -> None:
        """Write a typed event to the session log."""
        self.manager._append_event(self.session_id, event)

    def _wire(self) -> Any:
        try:
            from coderai.wire.emitter import get_emitter

            return get_emitter()
        except Exception:
            return None

    def emit_turn_start(self) -> None:
        try:
            self._turn = self.manager.next_turn(self.session_id)
        except Exception:
            self._turn += 1
        self._step = 0
        self._emit(make_turn_start(self._next_seq(), self._turn))
        try:
            w = self._wire()
            if w is not None:
                last_user = ""
                try:
                    msgs = self.manager.list_session_messages(self.session_id)
                    for m in reversed(msgs):
                        if m.role == "user":
                            last_user = str(m.content or "")[:4000]
                            break
                except Exception:
                    pass
                w.turn_begin(last_user)
        except Exception:
            pass
        from coderai.hooks.runner import run_pre_turn

        try:
            settings = self.manager.get_resolved_settings()
            run_pre_turn(
                turn=self._turn,
                session_id=self.session_id,
                project_root=self.manager.project_root,
                settings=settings,
            )
        except Exception:
            pass

    def emit_turn_end(self, reason: str) -> None:
        self._emit(make_turn_end(self._next_seq(), self._turn, reason))
        try:
            w = self._wire()
            if w is not None and reason not in ("permission", "question"):
                w.flush()
                w.turn_end()
        except Exception:
            pass
        if reason not in ("permission", "question"):
            from coderai.hooks.runner import run_post_turn, run_stop

            try:
                settings = self.manager.get_resolved_settings()
                run_post_turn(
                    turn=self._turn,
                    session_id=self.session_id,
                    project_root=self.manager.project_root,
                    reason=reason,
                    settings=settings,
                )
            except Exception:
                pass
            if reason in ("waiting", "max_steps", "refusal", "natural"):
                try:
                    settings = self.manager.get_resolved_settings()
                    outcome = run_stop(
                        self.session_id,
                        self.manager.project_root,
                        settings=settings,
                    )
                    if outcome and (outcome.stop or outcome.decision == "deny"):
                        for ctx in outcome.additional_context:
                            try:
                                self.manager._append_message(
                                    self.manager._build_message(
                                        self.session_id,
                                        "user",
                                        str(ctx),
                                        meta={"isHookContext": True},
                                    )
                                )
                            except Exception:
                                pass
                except Exception:
                    pass

    def emit_step_start(self) -> None:
        try:
            self._step = self.manager.next_step(self.session_id)
        except Exception:
            self._step += 1
        self._emit(make_step_start(self._next_seq(), self._turn, self._step))
        try:
            w = self._wire()
            if w is not None:
                w.step_begin(self._step)
        except Exception:
            pass

    def emit_step_end(self) -> None:
        self._emit(make_step_end(self._next_seq(), self._turn, self._step))

    async def run(
        self,
        permission_replies: list[dict[str, Any]] | None = None,
        deferred_prompt: str | None = None,
    ) -> None:
        """Run one activation until completion, pause, interruption, or iteration limit."""
        from coderai.soul.session.manager import (
            _accumulate_usage,
            _accumulate_usage_per_model,
            _normalize_tool_calls,
            _now,
            _total_tokens,
            sanitize_repetition_loops,
        )

        manager = self.manager
        session_id = self.session_id
        started_at_ms = int(time.time() * 1000)
        client_info = manager.create_openai_client()
        client = client_info.get("client")
        model = manager.get_active_model()
        wire_model = client_info.get("model") or model
        base_url = client_info.get("baseURL")
        temperature = client_info.get("temperature")
        thinking_enabled = bool(client_info.get("thinkingEnabled"))
        reasoning_effort = (
            manager.get_reasoning_effort() or client_info.get("reasoningEffort") or "max"
        )
        settings = manager.get_resolved_settings()

        self._claim_controller()
        if permission_replies is None:
            self.emit_turn_start()

        messages = manager.list_session_messages(session_id)
        rebuild_session_state_from_history(
            session_id, [manager._serialize_message(message) for message in messages]
        )

        manager._update_entry(
            session_id,
            lambda entry: {
                **entry,
                "status": "processing",
                "failReason": None,
                "updateTime": _now(),
            },
        )

        if client is None:
            manager._update_entry(
                session_id,
                lambda entry: {
                    **entry,
                    "status": "failed",
                    "failReason": "API key not found",
                    "updateTime": _now(),
                },
            )
            manager.on_assistant_message(
                manager._build_message(
                    session_id,
                    "assistant",
                    "API key not found. Set your API key in .env (e.g. OPENAI_API_KEY=...), "
                    "export it in your shell, or configure ~/.coderai/settings.json.",
                ),
                False,
            )
            self._release_controller()
            return

        try:
            for _iteration in range(manager.max_iterations):
                if manager.is_interrupted(session_id):
                    return

                manager._dispatch_due_schedules(session_id)
                messages = manager.list_session_messages(session_id)

                pending_info = manager.message_converter.get_trailing_pending_tool_call_message(
                    messages
                )
                if pending_info.get("toolCalls"):
                    last_message = pending_info.get("message")
                    tool_calls = pending_info.get("toolCalls") or []
                    message_permissions = (
                        (last_message.meta or {}).get("askPermissions") if last_message else None
                    )
                    waiting = await manager._append_tool_messages(
                        session_id,
                        tool_calls,
                        permission_replies=permission_replies,
                        message_permissions=message_permissions,
                    )
                    permission_replies = None

                    if deferred_prompt:
                        manager.file_history.ensure_session(session_id)
                        checkpoint = manager.file_history.record_tracked_files_checkpoint(
                            session_id, "User prompt checkpoint"
                        )
                        manager._append_message(
                            manager._build_message(
                                session_id,
                                "user",
                                deferred_prompt,
                                meta={"checkpointHash": checkpoint.checkpoint_hash},
                            )
                        )
                        deferred_prompt = None

                    if manager.is_interrupted(session_id):
                        return
                    if waiting:
                        manager._update_entry(
                            session_id,
                            lambda entry: {
                                **entry,
                                "toolCalls": tool_calls,
                                "status": "waiting_for_user",
                                "updateTime": _now(),
                            },
                        )
                        return
                    continue

                current_entry = manager._get_entry(session_id) or {}
                active_tokens = current_entry.get("activeTokens", 0)
                budget = calculate_context_budget(model)
                auto_compact_threshold = (
                    settings.get("autoCompactWindow") or budget["pressure_threshold"]
                )
                # Dual trigger with reserved budget + ratio
                trigger = evaluate_compaction_trigger(
                    active_tokens,
                    budget["context_limit"],
                    pressure_ratio=settings.get("compactionTriggerRatio", 0.85),
                    overflow_ratio=0.95,
                    reserved_context_size=settings.get("reservedContextSize", 50000),
                )
                # ponytail: max_steps_per_turn guard prevents cost bomb
                max_steps = int(settings.get("maxStepsPerTurn") or 1000)
                if self._step >= max_steps:
                    self.emit_turn_end("max_steps")
                    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    manager._update_entry(
                        session_id,
                        lambda e: {**e, "status": "completed", "updateTime": now_iso},
                    )
                    manager.on_assistant_message(
                        manager._build_message(
                            session_id,
                            "assistant",
                            f"Reached max steps per turn ({max_steps}). Continuing requires a new turn.",
                        ),
                        False,
                    )
                    return
                if active_tokens > auto_compact_threshold or trigger is not None:
                    effective_trigger = trigger or "pressure"
                    compact_notice = manager._build_assistant(
                        session_id,
                        f"The conversation is getting long ({effective_trigger} trigger), "
                        "compacting...",
                        None,
                    )
                    compact_notice.meta = {"asThinking": True}
                    manager.on_assistant_message(compact_notice, False)
                    await manager._compact_session(session_id, trigger=effective_trigger)
                    messages = manager.list_session_messages(session_id)

                steers = manager.pop_steers(session_id)
                if steers:
                    for st in steers:
                        manager._append_message(
                            manager._build_message(
                                session_id,
                                "user",
                                st,
                            )
                        )
                    messages = manager.list_session_messages(session_id)

                self.emit_step_start()

                multimodal_mode = settings.get("multimodal", "default")
                tools_preset = settings.get("toolsPreset") or settings.get("preset")
                tools = get_tools(
                    {
                        "model": model,
                        "nonInteractive": manager.non_interactive,
                        "multimodal": multimodal_mode,
                        "preset": tools_preset,
                    },
                    external_tools=manager.get_external_tool_definitions(tools_preset),
                )
                if tools:
                    tools = format_tool_definitions(tools, model=model)
                    from coderai.utils.common.message_converter import (
                        apply_tool_cache_control,
                        is_cache_control_supported,
                    )

                    if is_cache_control_supported(model):
                        tools = apply_tool_cache_control(tools, model)
                converted = manager.message_converter.convert_session_messages(
                    messages, model, thinking_enabled=thinking_enabled
                )
                # Phase 2: dynamic injections (collect injections before each LLM step).
                # Persist injections linearly into session history so the KV cache prefix
                # remains strictly monotonic and does not fork on subsequent turns.
                try:
                    from coderai.soul.dynamic_injection import wrap_as_reminder

                    soul = manager.get_soul(session_id)
                    collected = await soul.collect_injections()
                    for injection in collected:
                        wrapped = wrap_as_reminder(injection.content)
                        msg = manager._build_message(
                            session_id,
                            "user",
                            wrapped,
                            meta={"isDynamicInjection": True, "injectionType": injection.type},
                        )
                        manager._append_message(msg)
                        converted.append({"role": "user", "content": wrapped})
                except Exception:
                    pass
                request: dict[str, Any] = {
                    "model": wire_model,
                    "messages": converted,
                    "tools": tools if tools else None,
                }
                if temperature is not None:
                    request["temperature"] = temperature
                effective_reasoning = reasoning_effort
                if effective_reasoning in ("adaptive", "auto", None) or (
                    is_fast_model(model)
                    and effective_reasoning == "max"
                    and not settings.get("explicitReasoningEffort")
                ):
                    effective_reasoning = resolve_adaptive_reasoning_effort(
                        model,
                        turn=self.turn,
                        step=self.step,
                        explicit_effort=settings.get("explicitReasoningEffort"),
                    )
                request.update(
                    build_thinking_request_options(
                        thinking_enabled,
                        base_url=base_url,
                        reasoning_effort=effective_reasoning,
                        model=model,
                        has_tools=bool(request.get("tools")),
                    )
                )
                if not request.get("tools"):
                    request.pop("tools", None)

                try:
                    response = await manager._create_completion_with_retry(
                        session_id, client, request
                    )
                except Exception as error:
                    if manager.is_interrupted(session_id):
                        return
                    error_text = describe_llm_error(error)
                    log_api_error(error, {"sessionId": session_id, "model": model})
                    manager._update_entry(
                        session_id,
                        lambda entry: {
                            **entry,
                            "status": "failed",
                            "failReason": error_text,
                            "updateTime": _now(),
                        },
                    )
                    manager.on_assistant_message(
                        manager._build_message(
                            session_id,
                            "assistant",
                            f"Request failed: {error_text}",
                        ),
                        False,
                    )
                    self.emit_step_end()
                    self.emit_turn_end("error")
                    return

                if manager.is_interrupted(session_id):
                    self.emit_step_end()
                    self.emit_turn_end("interrupted")
                    return

                choice = (response.get("choices") or [{}])[0]
                response_message = choice.get("message") or {}
                raw_content = response_message.get("content") or ""
                content = (
                    sanitize_repetition_loops(raw_content) if isinstance(raw_content, str) else ""
                )
                tool_calls = _normalize_tool_calls(response_message.get("tool_calls"))
                thinking = response_message.get("reasoning_content")
                refusal = response_message.get("refusal")
                usage = response.get("usage")
                total_active = _total_tokens(usage)
                extracted_usage = extract_usage_dict(usage) if usage else None

                assistant_message = manager._build_assistant(
                    session_id, content, tool_calls, thinking
                )
                assistant_message.meta = {
                    **(assistant_message.meta or {}),
                    "usage": extracted_usage,
                    "model": model,
                }

                current_entry = manager._get_entry(session_id) or {}
                is_plan = bool(current_entry.get("planMode"))
                forced_scopes = PLAN_MODE_FORCE_ASK_SCOPES if is_plan else None
                custom_paths = settings.get("skillScanPaths") or []
                permission_plan = (
                    compute_tool_call_permissions(
                        session_id=session_id,
                        project_root=manager.project_root,
                        tool_calls=tool_calls,
                        settings=settings.get("permissions") or {},
                        force_ask_scopes=forced_scopes,
                        read_permission_exempt_paths=get_skill_read_exempt_paths(
                            manager.project_root, custom_scan_paths=custom_paths
                        ),
                        resolve_snippet_path=resolve_snippet_file_path,
                    )
                    if tool_calls
                    else None
                )
                # YOLO/AFK parity: auto-approve suppresses askPermissions
                if (
                    manager.is_auto_approve()
                    and permission_plan
                    and permission_plan.get("askPermissions")
                ):
                    # Convert ask -> allow for all tool calls
                    permission_plan = {
                        "permissions": [
                            {"toolCallId": p.get("toolCallId"), "permission": "allow"}
                            for p in permission_plan.get("permissions", [])
                        ],
                        "askPermissions": None,
                    }
                    assistant_message.meta = {
                        **(assistant_message.meta or {}),
                        "permissions": permission_plan["permissions"],
                        "askPermissions": None,
                    }

                if permission_plan and permission_plan.get("permissions"):
                    assistant_message.meta = {
                        **(assistant_message.meta or {}),
                        "permissions": permission_plan["permissions"],
                        "askPermissions": permission_plan.get("askPermissions"),
                    }

                manager._append_message(assistant_message)
                manager.on_assistant_message(assistant_message, True)

                waiting_for_user = False
                if tool_calls:
                    if permission_plan and permission_plan.get("askPermissions"):
                        manager._update_entry(
                            session_id,
                            lambda entry: {
                                **entry,
                                "assistantReply": content,
                                "assistantThinking": thinking,
                                "assistantRefusal": refusal,
                                "toolCalls": tool_calls,
                                "usage": _accumulate_usage(entry.get("usage"), usage),
                                "usagePerModel": _accumulate_usage_per_model(
                                    entry.get("usagePerModel"), model, usage
                                ),
                                "activeTokens": total_active or entry.get("activeTokens", 0),
                                "status": "ask_permission",
                                "failReason": None,
                                "askPermissions": permission_plan["askPermissions"],
                                "updateTime": _now(),
                            },
                        )
                        self.emit_step_end()
                        self.emit_turn_end("permission")
                        return

                    waiting_for_user = await manager._append_tool_messages(
                        session_id,
                        tool_calls,
                        permission_replies=None,
                        message_permissions=(
                            permission_plan.get("permissions") if permission_plan else None
                        ),
                    )

                if manager.is_interrupted(session_id):
                    self.emit_step_end()
                    self.emit_turn_end("interrupted")
                    return

                new_status = (
                    "failed"
                    if refusal
                    else "waiting_for_user"
                    if waiting_for_user
                    else "processing"
                    if tool_calls
                    else "completed"
                )
                manager._update_entry(
                    session_id,
                    lambda entry: {
                        **entry,
                        "assistantReply": content,
                        "assistantThinking": thinking,
                        "assistantRefusal": refusal,
                        "toolCalls": tool_calls,
                        "usage": _accumulate_usage(entry.get("usage"), usage),
                        "usagePerModel": _accumulate_usage_per_model(
                            entry.get("usagePerModel"), model, usage
                        ),
                        "activeTokens": total_active or entry.get("activeTokens", 0),
                        "status": new_status,
                        "failReason": refusal if refusal else entry.get("failReason"),
                        "askPermissions": None,
                        "updateTime": _now(),
                    },
                )

                if refusal or waiting_for_user:
                    self.emit_step_end()
                    self.emit_turn_end("refusal" if refusal else "waiting")
                    return
                if not tool_calls:
                    self.emit_step_end()
                    self.emit_turn_end("natural")
                    return
                self.emit_step_end()

            self.emit_turn_end("max_iterations")
            manager._update_entry(
                session_id,
                lambda entry: {
                    **entry,
                    "status": "completed",
                    "updateTime": _now(),
                },
            )
            manager.on_assistant_message(
                manager._build_assistant(
                    session_id,
                    "The AI agent has taken several steps but hasn't reached a conclusion yet. "
                    "Do you want to continue?",
                    None,
                ),
                False,
            )
        except asyncio.CancelledError:
            self.emit_turn_end("cancelled")
            manager._update_entry(
                session_id,
                lambda entry: {
                    **entry,
                    "status": "interrupted",
                    "failReason": "interrupted",
                    "updateTime": _now(),
                },
            )
        finally:
            self._release_controller()
            manager.maybe_notify_task_completion(session_id, started_at_ms)

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def step(self) -> int:
        return self._step

