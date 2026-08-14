# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Concurrency policy and phased scheduling for tool batches."""

import asyncio
import logging
import time as _time
from typing import Any, Optional

from coderAI.core.execution_context import resolve_delegation_isolation_domain
from coderAI.core.services import get_services
from coderAI.types.tool_error_codes import ToolErrorCode

DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS = 4
logger = logging.getLogger(__name__)


class BatchScheduler:
    def _mutating_subagent_cap(self) -> int:
        cfg = self.runtime.config
        # If the user explicitly set the config key (project or user config),
        # honour it. Otherwise auto-scale from CPU count so the default on an
        # 8-core machine is higher than on a 2-core CI runner.
        explicit = False
        if cfg is not None:
            if hasattr(cfg, "model_fields_set"):
                explicit = "max_concurrent_mutating_subagents" in getattr(
                    cfg, "model_fields_set", set()
                )
            else:
                # Test helpers use SimpleNamespace without model_fields_set
                explicit = hasattr(cfg, "max_concurrent_mutating_subagents")
        if not explicit:
            try:
                import os

                cpus = os.cpu_count() or 4
                auto = max(4, min(8, cpus))
                return auto
            except Exception:
                return DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS
        try:
            cap = int(
                getattr(
                    cfg,
                    "max_concurrent_mutating_subagents",
                    DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS,
                )
            )
            return max(1, min(10, cap))
        except (TypeError, ValueError):
            return DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS

    @property
    def _read_only_semaphore(self) -> asyncio.Semaphore:
        return self._ro_semaphore

    @property
    def _read_only_subagent_semaphore(self) -> asyncio.Semaphore:
        return self._subagent_ro_semaphore

    async def run_tool_batch(
        self, parsed_calls: list, hooks_data: Optional[dict[str, Any]], hooks_manager: Any
    ) -> list:
        results: list[Any] = [None] * len(parsed_calls)
        total, done = len(parsed_calls), 0
        # _cancel_event is an asyncio.Event on AgentTrackerInfo used to
        # signal cancellation across concurrent tool tasks.
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None

        precomputed_diffs = await self._precompute_diffs(parsed_calls)

        def _is_read_call(pc: dict[str, Any]) -> bool:
            if pc.get("tool_name") == "delegate_task" and isinstance(pc.get("arguments"), dict):
                return resolve_delegation_isolation_domain(pc["arguments"]) == "read_only"
            tool = self.agent.tools.get(pc.get("tool_name", ""))
            return bool(tool and getattr(tool, "is_read_only", False))

        def _cancelled_result() -> dict[str, Any]:
            return {
                "success": False,
                "error": "Cancelled by user.",
                "error_code": ToolErrorCode.CANCELLED,
            }

        async def _run(pc: dict[str, Any], diff: Optional[str] = None) -> dict[str, Any]:
            if not cancel_event:
                return await self.execute_single_tool(
                    pc, hooks_data, hooks_manager, precomputed_diff=diff
                )
            if cancel_event.is_set():
                return _cancelled_result()

            t = asyncio.create_task(
                self.execute_single_tool(pc, hooks_data, hooks_manager, precomputed_diff=diff)
            )
            w = asyncio.create_task(cancel_event.wait())
            done_set, _pending = await asyncio.wait({t, w}, return_when=asyncio.FIRST_COMPLETED)
            if t in done_set:
                w.cancel()
                await asyncio.gather(w, return_exceptions=True)
                return t.result()

            if not _is_read_call(pc):
                # asyncio cancellation cannot stop a mutation already running
                # in a worker thread. Let it settle rather than reporting a
                # cancellation while an untracked side effect continues.
                result = dict(await t)
                result["_cancellation_requested"] = True
                result["_warning"] = (
                    "Cancellation was requested after this mutating tool started; "
                    "the tool was allowed to finish to avoid an unreported background mutation."
                )
                get_services().events.emit(
                    "agent_warning",
                    message=f"Cancellation waited for mutating tool '{pc.get('tool_name', 'unknown')}'.",
                )
                return result

            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            return _cancelled_result()

        def _emit_progress(i: int, elapsed: Optional[float] = None) -> None:
            nonlocal done
            done += 1
            payload = {"step": done, "total": total, "tool_name": parsed_calls[i]["tool_name"]}
            if elapsed is not None:
                payload["elapsed"] = elapsed
            get_services().events.emit("tool_progress", **payload)

        def _coerce_gather_result(idx: int, raw: Any) -> dict[str, Any]:
            if isinstance(raw, BaseException):
                if isinstance(raw, (KeyboardInterrupt, SystemExit)):
                    raise raw
                tool_name = parsed_calls[idx].get("tool_name", "unknown")
                logger.warning("Tool '%s' raised in parallel batch: %s", tool_name, raw)
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' raised: {raw}",
                    "error_code": ToolErrorCode.TOOL_EXCEPTION,
                }
            if isinstance(raw, dict):
                return raw
            return {"success": True, "result": raw}

        def _phase_kind(pc: dict[str, Any]) -> str:
            if pc.get("tool_name") in ("manage_tasks", "request_plan_amendment") and _is_read_call(
                pc
            ):
                # High-impact: control tools get priority lane even among reads
                return "priority"
            if _is_read_call(pc):
                return "read"
            if pc.get("tool_name") == "delegate_task" and isinstance(pc.get("arguments"), dict):
                if resolve_delegation_isolation_domain(pc["arguments"]) == "browser":
                    return "browser"
            if pc.get("tool_name") in ("manage_tasks", "request_plan_amendment"):
                return "priority"
            return "mutation"

        async def _run_read(idx: int, caps: dict[str, asyncio.Semaphore]) -> dict[str, Any]:
            pc = parsed_calls[idx]
            tool_name = pc.get("tool_name", "")
            tool = self.agent.tools.get(tool_name)
            semaphore = self._read_only_semaphore
            if tool_name == "delegate_task":
                semaphore = self._read_only_subagent_semaphore
            async with semaphore:
                max_parallel = int(getattr(tool, "max_parallel_invocations", 0) or 0)
                if max_parallel > 0:
                    cap = caps.setdefault(tool_name, asyncio.Semaphore(max_parallel))
                    async with cap:
                        return await _run(pc, diff=precomputed_diffs.get(idx))
                return await _run(pc, diff=precomputed_diffs.get(idx))

        async def _run_browser(idx: int) -> dict[str, Any]:
            async with self._subagent_mut_semaphore:
                return await _run(parsed_calls[idx], diff=None)

        mutation_completed = False
        cursor = 0
        # High-impact priority lane: run manage_tasks first so completion gate
        # and objective ledger never stall behind file edits
        # Reorder: priority phase first, then reads, then browser/mutations
        priority_indices = [i for i, pc in enumerate(parsed_calls) if _phase_kind(pc) == "priority"]
        if priority_indices:
            for idx in priority_indices:
                t0 = _time.time()
                results[idx] = await _run(parsed_calls[idx], diff=None)
                _emit_progress(idx, elapsed=round(_time.time() - t0, 2))
            # Priority results already emitted — cursor loop skips them via kind check.
        while cursor < len(parsed_calls):
            kind = _phase_kind(parsed_calls[cursor])
            if kind == "priority":
                cursor += 1
                continue
            if kind in {"read", "browser"}:
                phase_indices: list[int] = []
                while cursor < len(parsed_calls) and _phase_kind(parsed_calls[cursor]) == kind:
                    phase_indices.append(cursor)
                    cursor += 1

                if kind == "read":
                    caps: dict[str, asyncio.Semaphore] = {}
                    raw_results = await asyncio.gather(
                        *(_run_read(idx, caps) for idx in phase_indices),
                        return_exceptions=True,
                    )
                    for idx, raw in zip(phase_indices, raw_results, strict=True):
                        results[idx] = _coerce_gather_result(idx, raw)
                        _emit_progress(idx)
                    continue

                if kind == "browser":
                    raw_results = await asyncio.gather(
                        *(_run_browser(idx) for idx in phase_indices),
                        return_exceptions=True,
                    )
                    for idx, raw in zip(phase_indices, raw_results, strict=True):
                        results[idx] = _coerce_gather_result(idx, raw)
                        _emit_progress(idx)
                    mutation_completed = True
                    continue

            idx = cursor
            cursor += 1
            t0 = _time.time()
            diff = precomputed_diffs.get(idx) if not mutation_completed else None
            results[idx] = await _run(parsed_calls[idx], diff=diff)
            _emit_progress(idx, elapsed=round(_time.time() - t0, 2))
            mutation_completed = True

        return results
