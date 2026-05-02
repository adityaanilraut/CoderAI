"""Tool execution and orchestration for the CoderAI agent.

Handles batch parallelism, read-only vs mutating limits, hook execution,
and UI confirmation.
"""

import asyncio
import hashlib
import json
import logging
import time as _time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .agent_tracker import AgentStatus
from .events import event_emitter
from .tool_routing import call_mcp_tool_by_function_name, is_mcp_function_name, coerce_tool_arguments

logger = logging.getLogger(__name__)

# Cap concurrent read-only tools to avoid OS resource exhaustion
MAX_CONCURRENT_READ_ONLY = 20

DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

# Cap concurrent read-only sub-agent delegations. Each sub-agent is a full
# LLM session with its own tool loop, so we fan out far less aggressively
# than for cheap read-only tools like read_file / grep.
MAX_CONCURRENT_READ_ONLY_SUBAGENTS = 4

# Number of times an identical (tool, args) call may be repeated across
# iterations before the executor intervenes and returns a cached-with-warning
# result. Stops the model from looping on the same read_file / git_status.
DUPLICATE_CALL_THRESHOLD = 2

# Hard ceiling on identical (tool, args) calls across iterations. Once a
# fingerprint reaches this count the executor signals the loop to stop
# entirely. Unlike DUPLICATE_CALL_THRESHOLD this applies to ALL tools
# (read-only or not), because mutating tools called identically N times
# almost always indicate a stuck model rather than legitimate work.
# Triggered in production by gpt-5.4-mini calling `plan action=show`
# 14+ times in a single turn before the user cancelled.
DOOM_LOOP_HARD_THRESHOLD = 5


def _fingerprint(tool_name: str, arguments: Optional[Dict[str, Any]]) -> str:
    try:
        args_blob = json.dumps(arguments or {}, sort_keys=True, default=str)
    except Exception:
        args_blob = repr(arguments)
    h = hashlib.sha256(f"{tool_name}\x00{args_blob}".encode("utf-8")).hexdigest()
    return h


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent
        self._ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY)
        self._subagent_ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY_SUBAGENTS)
        self._call_counts: Dict[str, int] = {}
        self._last_results: Dict[str, Dict[str, Any]] = {}
        self._preview_file_cache: Dict[str, Tuple[float, str]] = {}

    def reset_counts(self) -> None:
        self._call_counts.clear()
        self._last_results.clear()
        self._preview_file_cache.clear()

    def _approval_allowlist(self) -> set[str]:
        # Initialized in Agent.__init__ so no race-condition risk here.
        return self.agent._tool_approval_allowlist

    def _normalize_tool_result(
        self,
        result: Any,
        *,
        tool_name: str,
        default_error_code: str = "tool_error",
    ) -> Dict[str, Any]:
        if isinstance(result, dict):
            normalized = dict(result)
            if "success" not in normalized:
                has_useful_output = bool(normalized.get("result") or normalized.get("output") or normalized.get("data"))
                normalized["success"] = "error" not in normalized and has_useful_output
            if normalized.get("success") is False:
                normalized["error"] = str(
                    normalized.get("error") or f"Tool '{tool_name}' failed."
                )
                normalized.setdefault("error_code", default_error_code)
            return normalized

        if isinstance(result, str):
            return {
                "success": False,
                "error": result,
                "error_code": default_error_code,
            }

        if result is None:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' returned no result.",
                "error_code": default_error_code,
            }

        return {"success": True, "result": result}

    def _enter_waiting_for_user(
        self, tool_name: str
    ) -> Optional[Tuple[AgentStatus, Optional[str]]]:
        info = self.agent.tracker_info
        if not info:
            return None
        previous = (info.status, info.current_tool)
        info.status = AgentStatus.WAITING_FOR_USER
        info.current_tool = tool_name
        self.agent._sync_tracker()
        return previous

    def _exit_waiting_for_user(
        self, previous: Optional[Tuple[AgentStatus, Optional[str]]]
    ) -> None:
        info = self.agent.tracker_info
        if not info or previous is None:
            return
        if info.status == AgentStatus.CANCELLED:
            self.agent._sync_tracker()
            return
        prev_status, prev_tool = previous
        info.status = prev_status
        info.current_tool = prev_tool
        self.agent._sync_tracker()

    @property
    def _read_only_semaphore(self) -> asyncio.Semaphore:
        return self._ro_semaphore

    @property
    def _read_only_subagent_semaphore(self) -> asyncio.Semaphore:
        return self._subagent_ro_semaphore

    def _compute_preview_diff(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        if tool_name not in ("write_file", "search_replace", "apply_diff", "multi_edit"):
            return None

        path = arguments.get("path")
        if not path:
            return None

        from pathlib import Path
        import difflib

        try:
            path_obj = Path(path).expanduser().resolve()

            import os
            if self.agent and self.agent.config and os.environ.get("CODERAI_ALLOW_OUTSIDE_PROJECT") != "1":
                project_root = Path(self.agent.config.project_root).resolve()
                try:
                    path_obj.relative_to(project_root)
                except ValueError:
                    return None

            if not path_obj.exists() and tool_name != "write_file":
                return None

            original_content = ""
            if path_obj.exists():
                try:
                    resolved = str(path_obj.resolve())
                    current_mtime = path_obj.stat().st_mtime
                    cached = self._preview_file_cache.get(resolved)
                    if cached is not None and cached[0] == current_mtime:
                        original_content = cached[1]
                    else:
                        original_content = path_obj.read_text(encoding="utf-8")
                        self._preview_file_cache[resolved] = (current_mtime, original_content)
                except Exception:
                    return None

            new_content = original_content

            if tool_name == "write_file":
                if arguments.get("append"):
                    new_content += arguments.get("content", "")
                else:
                    new_content = arguments.get("content", "")
            elif tool_name == "search_replace":
                search = arguments.get("search", "")
                replace = arguments.get("replace", "")
                if arguments.get("replace_all"):
                    new_content = original_content.replace(search, replace)
                else:
                    new_content = original_content.replace(search, replace, 1)
            elif tool_name == "multi_edit":
                edits = arguments.get("edits", [])
                for edit in edits:
                    new_content = new_content.replace(edit.get("search", ""), edit.get("replace", ""))
            elif tool_name == "apply_diff":
                raw_diff = arguments.get("diff", "")
                if len(raw_diff) > 32768:
                    hidden = len(raw_diff) - 32768
                    return (
                        raw_diff[:32768]
                        + f"\n... (diff truncated) {hidden} chars hidden"
                    )
                return raw_diff
            
            diff_lines = list(
                difflib.unified_diff(
                    original_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{path_obj.name}",
                    tofile=f"b/{path_obj.name}",
                    n=3,
                )
            )
            diff_text = "".join(diff_lines)
            if len(diff_text) > 32768:
                hidden = len(diff_text) - 32768
                return (
                    diff_text[:32768]
                    + f"\n... (diff truncated) {hidden} chars hidden"
                )
            return diff_text
        except Exception as e:
            logger.debug("Preview diff computation failed for %s: %s", tool_name, e)
            return None

    async def _precompute_diffs(
        self, parsed_calls: list
    ) -> Dict[int, Optional[str]]:
        gated: List[Tuple[int, dict]] = []
        for i, pc in enumerate(parsed_calls):
            if pc.get("parse_error") or pc.get("arguments") is None:
                continue
            tool = self.agent.tools.get(pc.get("tool_name", ""))
            if tool is not None and getattr(tool, "requires_confirmation", False):
                gated.append((i, pc))

        if not gated:
            return {}

        async def _one(idx: int, pc: dict) -> Tuple[int, Optional[str]]:
            diff = await asyncio.to_thread(
                self._compute_preview_diff, pc["tool_name"], pc["arguments"]
            )
            return idx, diff

        diffs: Dict[int, Optional[str]] = {}
        results = await asyncio.gather(*(_one(i, pc) for i, pc in gated))
        for idx, diff in results:
            if diff is not None:
                diffs[idx] = diff
        return diffs

    async def _confirmation_callback(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_id: Optional[str] = None,
        precomputed_diff: Optional[str] = None,
    ) -> bool:
        # getattr avoids a hard import on ipc_server; the agent may run
        # without IPC (e.g. one-shot CLI) where ipc_server is never set.
        ipc_server = getattr(self.agent, "ipc_server", None)

        diff = (
            precomputed_diff
            if precomputed_diff is not None
            else await asyncio.to_thread(self._compute_preview_diff, tool_name, arguments)
        )

        if ipc_server is None:
            args_preview = json.dumps(arguments, indent=2)
            if len(args_preview) > 300:
                args_preview = args_preview[:300] + "\n  ... (truncated)"
            
            diff_preview = f"\n\nDiff Preview:\n{diff}" if diff else ""
            
            event_emitter.emit(
                "agent_status",
                message=(
                    f"\n[bold yellow]⚠ Tool '{tool_name}' requires confirmation.[/bold yellow]"
                    f"\n[dim]{args_preview}[/dim]"
                    f"[dim]{diff_preview}[/dim]"
                ),
            )

        previous = self._enter_waiting_for_user(tool_name)
        try:
            if ipc_server is not None:
                return await ipc_server.request_tool_approval(
                    tool_id=tool_id or str(uuid.uuid4()),
                    tool_name=tool_name,
                    arguments=arguments,
                    diff=diff,
                )

            try:
                from prompt_toolkit import PromptSession
                prompt_session = PromptSession()
                answer = await prompt_session.prompt_async("Allow this tool? (y/n) > ")
            except (ImportError, EOFError, KeyboardInterrupt):
                try:
                    loop = asyncio.get_running_loop()
                    answer = await loop.run_in_executor(None, lambda: input("Allow this tool? (y/n) > "))
                except (EOFError, KeyboardInterrupt):
                    answer = "n"

            return answer.strip().lower() in ("y", "yes")
        finally:
            self._exit_waiting_for_user(previous)

    async def execute_single_tool(
        self,
        pc: Dict[str, Any],
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager,
        precomputed_diff: Optional[str] = None,
    ) -> Dict[str, Any]:
        if pc.get("parse_error"):
            return self._normalize_tool_result(
                {
                    "success": False,
                    "error": pc["parse_error"],
                    "error_code": "parse_error",
                },
                tool_name=pc.get("tool_name", "unknown"),
            )
        try:
            tool_name = pc["tool_name"]
            arguments = pc["arguments"]
            tool = self.agent.tools.get(tool_name)

            async def _confirm(name, args):
                return await self._confirmation_callback(
                    name, args, tool_id=pc["tool_id"], precomputed_diff=precomputed_diff
                )

            is_mcp_proxy = is_mcp_function_name(tool_name) and tool is None
            needs_confirmation = (
                not self.agent.auto_approve
                and tool_name not in self._approval_allowlist()
                and (
                    is_mcp_proxy
                    or bool(tool and getattr(tool, "requires_confirmation", False))
                )
            )
            if needs_confirmation:
                # Check permission hooks first (can auto-allow or auto-deny)
                if hooks_manager is not None and hooks_data:
                    permission_status = await getattr(
                        hooks_manager, "run_permission_hooks", lambda *a, **kw: None
                    )(tool_name, arguments, hooks_data)
                    if permission_status == "allow":
                        pass  # Skip user prompt, proceed
                    elif permission_status == "deny":
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' was denied by a permission hook.",
                            "error_code": "denied_by_hook",
                        }
                    else:
                        approved = await _confirm(tool_name, arguments)
                        if not approved:
                            return {
                                "success": False,
                                "error": f"Tool '{tool_name}' was denied by the user.",
                                "error_code": "denied",
                            }
                else:
                    approved = await _confirm(tool_name, arguments)
                    if not approved:
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' was denied by the user.",
                            "error_code": "denied",
                        }

            pre_hooks = await hooks_manager.run_hooks(
                tool_name, "PreToolUse", arguments, hooks_data
            ) or []
            for hook_msg in pre_hooks:
                if hook_msg.startswith("[PreToolUse Hook ERROR]"):
                    return {
                        "success": False,
                        "error": hook_msg,
                        "error_code": "hook_blocked",
                    }

            timeout = getattr(tool, "timeout", None) or DEFAULT_TOOL_TIMEOUT_SECONDS

            async def _inner_execute():
                if is_mcp_proxy:
                    return await call_mcp_tool_by_function_name(tool_name, arguments)
                else:
                    return await self.agent.tools.execute(
                        tool_name,
                        confirmation_callback=None,
                        **arguments,
                    )

            tool_timed_out = False
            try:
                result = await asyncio.wait_for(_inner_execute(), timeout=timeout)
            except asyncio.TimeoutError:
                tool_timed_out = True
                result = {
                    "success": False,
                    "error": f"Tool '{tool_name}' exceeded timeout of {timeout}s",
                    "error_code": "timeout",
                }

            post_hook_args = dict(arguments or {})
            if tool_timed_out:
                post_hook_args["_tool_timed_out"] = True
            post_hooks = await hooks_manager.run_hooks(tool_name, "PostToolUse", post_hook_args, hooks_data) or []
            result = self._normalize_tool_result(result, tool_name=tool_name)

            if isinstance(result, dict) and (pre_hooks or post_hooks):
                result["_hooks"] = {"pre": pre_hooks, "post": post_hooks}
            return result
        except Exception as e:
            return self._normalize_tool_result(
                {
                    "success": False,
                    "error": str(e),
                    "error_code": "tool_exception",
                },
                tool_name=pc.get("tool_name", "unknown"),
            )

    async def orchestrate_tool_calls(
        self,
        tool_calls: list,
        messages: List[Dict[str, Any]],
        user_message: str,
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        parsed_calls = []
        parse_failures = 0
        for tc in tool_calls:
            tool_id = tc.get("id", "")
            func = tc.get("function", {}) or {}
            name = func.get("name", "") or ""
            raw_args = func.get("arguments")
            args, arg_err = coerce_tool_arguments(raw_args)
            if arg_err is not None:
                parse_failures += 1
                parsed_calls.append({"tool_id": tool_id, "tool_name": name, "arguments": None, "parse_error": arg_err})
            else:
                parsed_calls.append({"tool_id": tool_id, "tool_name": name, "arguments": args, "parse_error": None})

        if parse_failures == len(parsed_calls):
            # All tools failed to parse — record the synthetic tool replies and
            # ask the loop for another LLM round. The loop's
            # ``consecutive_errors`` counter terminates if this keeps happening.
            for pc in parsed_calls:
                self.agent.session.add_message(
                    "tool",
                    json.dumps(
                        {
                            "success": False,
                            "error": pc["parse_error"],
                            "error_code": "parse_error",
                        }
                    ),
                    tool_call_id=pc["tool_id"],
                    name=pc["tool_name"],
                )

            messages.clear()
            messages.extend(self.agent.session.get_messages_for_api())
            return True, {"retry": True}

        if self.agent.tracker_info:
            self.agent.tracker_info.status = AgentStatus.TOOL_CALL
            self.agent.tracker_info.current_tool = ", ".join(pc["tool_name"] for pc in parsed_calls if pc["arguments"])
            self.agent._sync_tracker()

        for pc in parsed_calls:
            if pc["parse_error"] is not None:
                event_emitter.emit("tool_error", tool_name=pc["tool_name"], error=pc["parse_error"])
            elif pc["arguments"] is not None:
                event_emitter.emit("tool_call", tool_name=pc["tool_name"], arguments=pc["arguments"], tool_id=pc["tool_id"])

        dup_results: Dict[int, Dict[str, Any]] = {}
        batch_seen: Dict[str, int] = {}
        to_run_indices: List[int] = []
        for idx, pc in enumerate(parsed_calls):
            if pc["parse_error"] is not None or pc["arguments"] is None:
                to_run_indices.append(idx)
                continue
            fp = _fingerprint(pc["tool_name"], pc["arguments"])
            pc["_fp"] = fp

            if fp in batch_seen:
                dup_results[idx] = {
                    "_dup_of_batch_index": batch_seen[fp],
                    "_warning": (
                        f"Duplicate call to '{pc['tool_name']}' in the same batch — "
                        "result reused from the first call. Avoid emitting identical "
                        "parallel tool calls."
                    ),
                }
                continue

            prior_count = self._call_counts.get(fp, 0)
            tool = self.agent.tools.get(pc["tool_name"])
            is_read_only = bool(tool and getattr(tool, "is_read_only", False))
            if is_read_only and prior_count >= DUPLICATE_CALL_THRESHOLD and fp in self._last_results:
                cached = dict(self._last_results[fp])
                cached["_warning"] = (
                    f"This is call #{prior_count + 1} to '{pc['tool_name']}' with identical "
                    "arguments — returning the cached result. Stop repeating the same read; "
                    "either work with the data you already have or try a different approach."
                )
                dup_results[idx] = cached
                event_emitter.emit(
                    "agent_warning",
                    message=f"Skipping duplicate read-only call to {pc['tool_name']} (already run {prior_count}×).",
                )
                continue

            batch_seen[fp] = idx
            to_run_indices.append(idx)

        calls_to_run = [parsed_calls[i] for i in to_run_indices]
        run_results = await self.run_tool_batch(calls_to_run, hooks_data, hooks_manager)

        # Merge real results + dup short-circuit results back into original order
        results: List[Any] = [None] * len(parsed_calls)
        for i, r in zip(to_run_indices, run_results):
            results[i] = r
        for i, placeholder in dup_results.items():
            src = placeholder.pop("_dup_of_batch_index", None)
            if src is not None and results[src] is not None:
                cloned = dict(results[src]) if isinstance(results[src], dict) else {"output": results[src]}
                cloned["_warning"] = placeholder.get("_warning", "Duplicate result reused.")
                results[i] = cloned
            else:
                placeholder["error"] = "Duplicate tool call skipped"
                results[i] = placeholder

        # Update call counters / last-result cache for future iterations.
        # Also detect cross-iteration doom-loops here: if any fingerprint
        # has now been called >= DOOM_LOOP_HARD_THRESHOLD times, we'll
        # signal the loop to terminate after persisting the current results.
        doom_offender: Optional[Tuple[str, int]] = None  # (tool_name, count)
        for pc, res in zip(parsed_calls, results):
            fp = pc.get("_fp")
            if not fp:
                continue
            res = self._normalize_tool_result(res, tool_name=pc["tool_name"])
            self._call_counts[fp] = self._call_counts.get(fp, 0) + 1
            if isinstance(res, dict) and res.get("success") is True:
                self._last_results[fp] = res
            count = self._call_counts[fp]
            if count >= DOOM_LOOP_HARD_THRESHOLD and (
                doom_offender is None or count > doom_offender[1]
            ):
                doom_offender = (pc["tool_name"], count)

        for pc, res in zip(parsed_calls, results):
            res = self.agent.context_controller.summarize_tool_result(res)
            res = self._normalize_tool_result(res, tool_name=pc["tool_name"])
            event_emitter.emit("tool_result", tool_name=pc["tool_name"], result=res, tool_id=pc["tool_id"])
            self.agent.session.add_message("tool", json.dumps(res), tool_call_id=pc["tool_id"], name=pc["tool_name"])

        if self.agent.tracker_info:
            self.agent.tracker_info.current_tool = None
            self.agent._sync_tracker()

        # Update the messages list from session
        messages.clear()
        messages.extend(self.agent.session.get_messages_for_api())

        # Detect which failures are user denials (not real errors).
        denied_tools: List[str] = []
        for pc, res in zip(parsed_calls, results):
            if isinstance(res, dict) and res.get("error_code") == "denied":
                denied_tools.append(pc.get("tool_name", "unknown"))

        all_tool_calls_failed = bool(results) and all(
            not (isinstance(res, dict) and res.get("success") is True)
            for res in results
        )
        if all_tool_calls_failed:
            if denied_tools:
                event_emitter.emit(
                    "agent_warning",
                    message=f"Tool(s) denied by user: {', '.join(denied_tools)}. "
                            "Asking the model to try a different approach.",
                )
                return True, {"retry": True, "_denied": True, "_denied_tools": denied_tools}
            event_emitter.emit(
                "agent_warning",
                message="All tool calls in this step failed. Asking the model to revise its plan.",
            )
            return True, {"retry": True}

        if denied_tools:
            return True, {"retry": True, "_denied": True, "_denied_tools": denied_tools}

        if doom_offender is not None:
            tool_name, count = doom_offender
            event_emitter.emit(
                "agent_warning",
                message=(
                    f"Stopping: '{tool_name}' was called {count} times with identical "
                    f"arguments. The model is stuck in a loop."
                ),
            )
            return True, {"_doom_loop_stop": True, "tool_name": tool_name, "count": count}

        return False, None

    async def run_tool_batch(self, parsed_calls: list, hooks_data: Optional[Dict[str, Any]], hooks_manager) -> list:
        ro_indices: list = []
        capped_groups: Dict[str, list] = {}
        sub_ro_indices: list = []
        mut_indices: list = []
        for i, pc in enumerate(parsed_calls):
            tool_name = pc["tool_name"]
            tool = self.agent.tools.get(tool_name)
            if not tool:
                mut_indices.append(i)
                continue
            # delegate_task with read_only_task=True is safe to parallelize.
            if (
                tool_name == "delegate_task"
                and isinstance(pc.get("arguments"), dict)
                and bool(pc["arguments"].get("read_only_task"))
            ):
                sub_ro_indices.append(i)
                continue
            max_par = getattr(tool, "max_parallel_invocations", 0)
            if max_par > 0:
                capped_groups.setdefault(tool_name, []).append(i)
            elif getattr(tool, "is_read_only", False):
                ro_indices.append(i)
            else:
                mut_indices.append(i)

        results = [None] * len(parsed_calls)
        total, done = len(parsed_calls), 0
        # _cancel_event is an asyncio.Event on AgentTrackerInfo used to
        # signal cancellation across concurrent tool tasks.
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None

        precomputed_diffs = await self._precompute_diffs(parsed_calls)

        async def _run(pc, diff=None):
            coro = self.execute_single_tool(pc, hooks_data, hooks_manager, precomputed_diff=diff)
            if not cancel_event:
                return await coro
            t = asyncio.ensure_future(coro)
            w = asyncio.ensure_future(cancel_event.wait())
            done_set, _pending = await asyncio.wait({t, w}, return_when=asyncio.FIRST_COMPLETED)
            if t in done_set:
                w.cancel()
                return t.result()
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            return {"success": False, "error": "Cancelled by user.", "error_code": "cancelled"}

        def _emit_progress(i: int, elapsed: Optional[float] = None) -> None:
            nonlocal done
            done += 1
            payload = {"step": done, "total": total, "tool_name": parsed_calls[i]["tool_name"]}
            if elapsed is not None:
                payload["elapsed"] = elapsed
            event_emitter.emit("tool_progress", **payload)

        def _coerce_gather_result(idx: int, raw: Any) -> Dict[str, Any]:
            if isinstance(raw, BaseException):
                if isinstance(raw, (KeyboardInterrupt, SystemExit)):
                    raise raw
                tool_name = parsed_calls[idx].get("tool_name", "unknown")
                logger.warning("Tool '%s' raised in parallel batch: %s", tool_name, raw)
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' raised: {raw}",
                    "error_code": "tool_exception",
                }
            return raw

        if ro_indices:
            async def _run_ro(idx):
                async with self._read_only_semaphore:
                    return await _run(parsed_calls[idx], diff=precomputed_diffs.get(idx))
            res = await asyncio.gather(
                *(_run_ro(i) for i in ro_indices), return_exceptions=True
            )
            for i, r in zip(ro_indices, res):
                results[i] = _coerce_gather_result(i, r)
                _emit_progress(i)

        if sub_ro_indices:
            async def _run_sub_ro(idx):
                async with self._read_only_subagent_semaphore:
                    return await _run(parsed_calls[idx], diff=precomputed_diffs.get(idx))
            res = await asyncio.gather(
                *(_run_sub_ro(i) for i in sub_ro_indices), return_exceptions=True
            )
            for i, r in zip(sub_ro_indices, res):
                results[i] = _coerce_gather_result(i, r)
                _emit_progress(i)

        for tool_name, indices in capped_groups.items():
            cap_tool = self.agent.tools.get(tool_name)
            size = max(1, int(getattr(cap_tool, "max_parallel_invocations", 1)))
            for start in range(0, len(indices), size):
                chunk = indices[start : start + size]
                res = await asyncio.gather(
                    *(_run(parsed_calls[i], diff=precomputed_diffs.get(i)) for i in chunk), return_exceptions=True
                )
                for i, r in zip(chunk, res):
                    results[i] = _coerce_gather_result(i, r)
                    _emit_progress(i)

        for i in mut_indices:
            t0 = _time.time()
            results[i] = await _run(parsed_calls[i], diff=precomputed_diffs.get(i))
            _emit_progress(i, elapsed=round(_time.time() - t0, 2))

        return results
