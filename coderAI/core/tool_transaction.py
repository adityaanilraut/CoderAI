# mypy: disable-error-code="attr-defined, has-type, no-any-return"
"""Permission-to-transaction bracket for a single tool invocation."""

import asyncio
import logging
from typing import Any, Optional

from coderAI.core.execution_context import (
    RunContext,
    execution_context_scope,
    get_execution_context,
    resolve_delegation_isolation_domain,
)
from coderAI.core.permissions import tool_requires_confirmation
from coderAI.core.services import get_services
from coderAI.core.tool_routing import is_mcp_function_name
from coderAI.core.workspace_transactions import WorkspaceTransactionError
from coderAI.system.error_policy import is_transient_error, is_transient_message
from coderAI.system.retry import backoff_delay
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.types.tool_results import normalize_tool_result

TOOL_RETRY_DELAY_CAP_SECONDS = 10.0
logger = logging.getLogger(__name__)


class TransactionBracket:
    async def _open_workspace_transaction(
        self,
        *,
        pc: dict[str, Any],
        tool_name: str,
        arguments: Any,
    ) -> tuple[Any, Any, Optional[dict[str, Any]]]:
        """Open the owning session ledger after every permission gate passes."""
        run_context = get_execution_context()
        store = run_context.transaction_store
        if store is None:
            # Lightweight executor tests and legacy integrations may execute
            # without a persisted session. A bound production session must
            # never mutate without its transaction store.
            if run_context.session_id is None:
                return None, None, None
            return (
                None,
                None,
                normalize_tool_result(
                    {
                        "success": False,
                        "error": "Workspace mutation blocked: session transaction ledger is unavailable.",
                        "error_code": ToolErrorCode.IO,
                    },
                    tool_name=tool_name,
                ),
            )
        objective_state = self._turn.objective_state
        try:
            handle = await asyncio.to_thread(
                store.begin,
                run_context=run_context,
                tool_call_id=str(pc.get("tool_id") or "unknown"),
                tool_name=tool_name,
                tool_arguments=arguments,
                objective=objective_state.objective if objective_state is not None else None,
                plan_id=self.runtime.active_plan_id,
                plan_revision=self.runtime.active_plan_revision,
            )
            return store, handle, None
        except (OSError, ValueError, WorkspaceTransactionError) as exc:
            return (
                None,
                None,
                normalize_tool_result(
                    {
                        "success": False,
                        "error": f"Workspace mutation blocked: {exc}",
                        "error_code": ToolErrorCode.IO,
                    },
                    tool_name=tool_name,
                ),
            )

    async def _finalize_workspace_transaction(
        self,
        result: dict[str, Any],
        *,
        store: Any,
        handle: Any,
        tool_name: str,
    ) -> dict[str, Any]:
        if store is None or handle is None:
            return result
        try:
            record = await asyncio.to_thread(
                store.finalize,
                handle,
                run_context=get_execution_context(),
                tool_result=result,
            )
        except Exception as exc:
            failed = dict(result)
            failed["success"] = False
            failed["error"] = (
                f"Tool '{tool_name}' finished, but its workspace transaction could not be "
                f"finalized: {exc}"
            )
            failed["error_code"] = ToolErrorCode.IO
            failed["_transaction_id"] = getattr(handle, "transaction_id", None)
            failed["_transaction_state"] = "partially_failed"
            return failed
        finalized = dict(result)
        finalized["_transaction_id"] = record.get("transaction_id")
        finalized["_transaction_state"] = record.get("state")
        finalized["_workspace_changes"] = [
            {"path": item.get("path"), "operation": item.get("operation")}
            for item in record.get("changes", [])
        ]
        if record.get("state") == "partially_failed":
            original_success = finalized.get("success") is True
            finalized["_tool_success"] = original_success
            finalized["success"] = False
            finalized["error"] = (
                "Workspace transaction recording was only partially successful; "
                "review the durable ledger before continuing."
            )
            finalized["error_code"] = ToolErrorCode.IO
        return finalized

    async def execute_single_tool(
        self,
        pc: dict[str, Any],
        hooks_data: Optional[dict[str, Any]],
        hooks_manager: Any,
        precomputed_diff: Optional[str] = None,
    ) -> dict[str, Any]:
        if pc.get("parse_error"):
            return normalize_tool_result(
                {
                    "success": False,
                    "error": pc["parse_error"],
                    "error_code": ToolErrorCode.PARSE_ERROR,
                },
                tool_name=pc.get("tool_name", "unknown"),
            )
        try:
            tool_name = pc["tool_name"]
            arguments = pc["arguments"]
            tool = self.agent.tools.get(tool_name)

            agent_id = "main"
            if self.agent.tracker_info and self.agent.tracker_info.agent_id:
                agent_id = self.agent.tracker_info.agent_id
            isolation_domain = None
            if tool_name == "delegate_task" and isinstance(arguments, dict):
                isolation_domain = resolve_delegation_isolation_domain(arguments)

            candidate_context = self.runtime.run_context
            run_context = candidate_context if isinstance(candidate_context, RunContext) else None
            with execution_context_scope(
                agent_id,
                isolation_domain=isolation_domain,
                run_context=run_context,
            ):
                return await self._execute_single_tool_inner(
                    pc,
                    hooks_data,
                    hooks_manager,
                    precomputed_diff=precomputed_diff,
                    tool=tool,
                    tool_name=tool_name,
                    arguments=arguments,
                )
        except Exception as e:
            return normalize_tool_result(
                {
                    "success": False,
                    "error": str(e),
                    "error_code": ToolErrorCode.TOOL_EXCEPTION,
                },
                tool_name=pc.get("tool_name", "unknown"),
            )

    async def _execute_single_tool_inner(
        self,
        pc: dict[str, Any],
        hooks_data: Optional[dict[str, Any]],
        hooks_manager: Any,
        *,
        precomputed_diff: Optional[str] = None,
        tool: Any = None,
        tool_name: str = "",
        arguments: Any = None,
    ) -> dict[str, Any]:
        try:
            is_mcp_proxy = is_mcp_function_name(tool_name) and tool is None
            active_plan_id = self.runtime.active_plan_id
            active_plan_revision = self.runtime.active_plan_revision
            is_mutating_call = bool(
                is_mcp_proxy or tool is None or not getattr(tool, "is_read_only", False)
            )
            if (
                active_plan_id
                and active_plan_revision
                and is_mutating_call
                and tool_name != "request_plan_amendment"
            ):
                if not self.runtime.plan_execution_ready:
                    return normalize_tool_result(
                        {
                            "success": False,
                            "error": (
                                f"Tool '{tool_name}' is blocked because this approved plan "
                                "execution was restored from session state but has not been "
                                "explicitly resumed. Use /plan resume or coderAI plan execute."
                            ),
                            "error_code": ToolErrorCode.PERMISSION_DENIED,
                        },
                        tool_name=tool_name,
                    )
                try:
                    from coderAI.core.planning import PlanStore

                    store = PlanStore(getattr(self.runtime.config, "project_root", ".") or ".")
                    active_record = store.load(str(active_plan_id))
                    execution_is_current = bool(
                        active_record is not None
                        and active_record.status == "executing"
                        and active_record.approved_revision == active_plan_revision
                        and active_record.revision == active_plan_revision
                    )
                except Exception:
                    execution_is_current = False
                if not execution_is_current:
                    return normalize_tool_result(
                        {
                            "success": False,
                            "error": (
                                f"Tool '{tool_name}' is blocked because the active approved plan "
                                "is no longer the executing revision. Review and reapprove the "
                                "current amendment before further mutations."
                            ),
                            "error_code": ToolErrorCode.PERMISSION_DENIED,
                        },
                        tool_name=tool_name,
                    )
            if self.runtime.plan_mode:
                read_only_delegation = (
                    tool_name == "delegate_task"
                    and isinstance(arguments, dict)
                    and bool(arguments.get("read_only_task"))
                )
                allowed_in_plan = bool(
                    tool_name == "submit_plan"
                    or read_only_delegation
                    or (tool is not None and getattr(tool, "is_read_only", False))
                )
                if not allowed_in_plan:
                    return normalize_tool_result(
                        {
                            "success": False,
                            "error": (
                                f"Tool '{tool_name}' is blocked by enforced Plan Mode. "
                                "Use read-only exploration and submit_plan."
                            ),
                            "error_code": ToolErrorCode.PERMISSION_DENIED,
                        },
                        tool_name=tool_name,
                    )
            allowed_native = self.runtime.allowed_native_tool_names
            if allowed_native is not None and (
                is_mcp_proxy or tool_name not in allowed_native or tool is None
            ):
                return normalize_tool_result(
                    {
                        "success": False,
                        "error": (
                            f"Tool '{tool_name}' is outside this sub-agent's "
                            f"{self.runtime.capability_domain} "
                            "capability boundary."
                        ),
                        "error_code": ToolErrorCode.PERMISSION_DENIED,
                    },
                    tool_name=tool_name,
                )
            # Confirmation-by-default (Phase 4.1): mutating tools require
            # confirmation unless they opt out with ``safe = True``; a tool that
            # declares nothing is treated as requiring confirmation. MCP proxy
            # calls (no local Tool object) always gate.
            needs_confirmation = (
                not self.runtime.auto_approve
                and not self._is_call_preapproved(tool_name, arguments)
                and (is_mcp_proxy or tool_requires_confirmation(tool))
            )
            # Egress gate (Phase 3.4): once this turn has ingested untrusted
            # external content, force confirmation for any network-egress tool —
            # even a read-only, allowlisted one — so injected page/MCP content
            # can't silently exfiltrate via a follow-up fetch. Deliberately
            # bypasses the name allowlist and the is_read_only fast-path, but
            # still honours the YOLO/auto_approve master switch.
            egress_gated = (
                not self.runtime.auto_approve
                and bool(tool and getattr(tool, "is_egress", False))
                and self._turn_has_untrusted()
            )
            if egress_gated:
                needs_confirmation = True
            # Confused-deputy gate (Phase 7.3): once this turn has ingested MCP
            # server output, a *local mutating* tool must get an explicit human
            # decision — even under auto_approve/--yolo — so a third-party MCP
            # server can't drive an unattended local write/exec. Unlike the egress
            # gate this survives auto_approve; it routes through the normal
            # confirmation path, which safely denies (deny-on-mutate override or
            # EOF at a non-interactive prompt) when there is no approver.
            mcp_mutation_gated = (
                tool is not None
                and not is_mcp_proxy
                and self._turn_has_untrusted_mcp()
                and not bool(getattr(tool, "is_read_only", False))
            )
            if mcp_mutation_gated:
                needs_confirmation = True

            async def _confirm(name: str, args: dict[str, Any]) -> bool:
                return await self._confirmation_callback(
                    name,
                    args,
                    tool_id=pc["tool_id"],
                    precomputed_diff=precomputed_diff,
                    force_confirm=mcp_mutation_gated,
                )

            if needs_confirmation:
                # Check permission hooks first (can auto-allow or auto-deny)
                if hooks_manager is not None and hooks_data:

                    async def fallback_hook(*a: Any, **kw: Any) -> Any:
                        return None

                    func = getattr(hooks_manager, "run_permission_hooks", fallback_hook)
                    permission_status = await func(tool_name, arguments, hooks_data)
                    if permission_status == "allow" and not mcp_mutation_gated:
                        pass  # Skip user prompt, proceed
                    elif permission_status == "deny":
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' was denied by a permission hook.",
                            "error_code": ToolErrorCode.DENIED_BY_HOOK,
                        }
                    else:
                        approved = await _confirm(tool_name, arguments)
                        if not approved:
                            return {
                                "success": False,
                                "error": f"Tool '{tool_name}' was denied by the user.",
                                "error_code": ToolErrorCode.DENIED,
                            }
                else:
                    approved = await _confirm(tool_name, arguments)
                    if not approved:
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' was denied by the user.",
                            "error_code": ToolErrorCode.DENIED,
                        }

            transaction_store = None
            transaction_handle = None
            # Workspace-mutating delegates now run in detached worktrees. The
            # parent transaction therefore observes only the reviewed patch
            # integration (plus parent on_subagent_stop hooks), never the
            # child's isolated intermediate mutations. Read-only/browser/
            # desktop delegations still do not open a parent workspace record.
            records_parent_workspace = bool(
                tool_name == "delegate_task"
                and isinstance(arguments, dict)
                and resolve_delegation_isolation_domain(arguments) == "workspace"
            )
            if is_mutating_call and (tool_name != "delegate_task" or records_parent_workspace):
                (
                    transaction_store,
                    transaction_handle,
                    transaction_error,
                ) = await self._open_workspace_transaction(
                    pc=pc,
                    tool_name=tool_name,
                    arguments=arguments,
                )
                if transaction_error is not None:
                    return transaction_error

            pre_hooks = (
                await hooks_manager.run_hooks(tool_name, "PreToolUse", arguments, hooks_data) or []
            )
            for hook_msg in pre_hooks:
                if hook_msg.startswith("[PreToolUse Hook ERROR]"):
                    blocked = normalize_tool_result(
                        {
                            "success": False,
                            "error": hook_msg,
                            "error_code": ToolErrorCode.HOOK_BLOCKED,
                        },
                        tool_name=tool_name,
                    )
                    return await self._finalize_workspace_transaction(
                        blocked,
                        store=transaction_store,
                        handle=transaction_handle,
                        tool_name=tool_name,
                    )

            from coderAI.core import tool_executor as _tool_executor

            timeout = _tool_executor.resolve_tool_timeout(tool, tool_name, arguments)

            async def _inner_execute() -> Any:
                if is_mcp_proxy:
                    return await _tool_executor.call_mcp_tool_by_function_name(tool_name, arguments)
                else:
                    return await self.agent.tools.execute(
                        tool_name,
                        **arguments,
                    )

            # Transient-failure retries (opt-in): only for tools that declare
            # ``retryable = True``, and never for a call that needed
            # confirmation (a single approval must not cover a second, unseen
            # attempt) or an MCP proxy call (third-party side effects are
            # unknowable). The gate above and PreToolUse hooks run once per
            # call; PostToolUse hooks run once on the final result.
            attempts_allowed = 1
            retry_base_delay = 1.0
            if (
                tool is not None
                and getattr(tool, "retryable", False)
                and self._idempotent(tool)
                and not needs_confirmation
                and not is_mcp_proxy
            ):
                try:
                    cfg = get_services().config
                    attempts_allowed = 1 + max(0, int(getattr(cfg, "tool_retry_max_attempts", 2)))
                    retry_base_delay = float(getattr(cfg, "tool_retry_base_delay", 1.0))
                except Exception:
                    attempts_allowed = 3

            cancel_event = (
                self.agent.tracker_info._cancel_event if self.agent.tracker_info else None
            )

            def _cancelled() -> bool:
                try:
                    return cancel_event is not None and bool(cancel_event.is_set())
                except Exception:
                    return False

            async def _retry_pause(attempt: int, why: str) -> bool:
                delay = backoff_delay(
                    attempt, base=retry_base_delay, cap=TOOL_RETRY_DELAY_CAP_SECONDS
                )
                message = (
                    f"Tool '{tool_name}' hit a transient failure "
                    f"(attempt {attempt}/{attempts_allowed}) — retrying in {delay:.1f}s: {why}"
                )
                logger.warning(message)
                get_services().events.emit("agent_warning", message=message)
                if cancel_event is None:
                    await asyncio.sleep(delay)
                    return True
                try:
                    await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    return False
                except asyncio.TimeoutError:
                    return True

            tool_timed_out = False
            result: Any = None
            for attempt in range(1, attempts_allowed + 1):
                try:
                    result = await asyncio.wait_for(_inner_execute(), timeout=timeout)
                except asyncio.TimeoutError:
                    # The executor's own timeout is never retried: a call that
                    # already proved slow would just burn another full budget.
                    tool_timed_out = True
                    result = {
                        "success": False,
                        "error": f"Tool '{tool_name}' exceeded timeout of {timeout}s",
                        "error_code": ToolErrorCode.TIMEOUT,
                    }
                    break
                except Exception as e:
                    if attempt < attempts_allowed and not _cancelled() and is_transient_error(e):
                        if await _retry_pause(attempt, str(e)):
                            continue
                        result = {
                            "success": False,
                            "error": f"Tool '{tool_name}' cancelled during retry backoff",
                            "error_code": ToolErrorCode.CANCELLED,
                        }
                        break
                    raise
                if (
                    attempt < attempts_allowed
                    and isinstance(result, dict)
                    and result.get("success") is False
                    and not _cancelled()
                    and is_transient_message(str(result.get("error") or ""))
                ):
                    if await _retry_pause(attempt, str(result.get("error") or "")):
                        continue
                    result = {
                        "success": False,
                        "error": f"Tool '{tool_name}' cancelled during retry backoff",
                        "error_code": ToolErrorCode.CANCELLED,
                    }
                    break
                break

            post_hook_args = dict(arguments or {})
            if tool_timed_out:
                post_hook_args["_tool_timed_out"] = True
            post_hooks = (
                await hooks_manager.run_hooks(tool_name, "PostToolUse", post_hook_args, hooks_data)
                or []
            )
            normalized_res: dict[str, Any] = normalize_tool_result(result, tool_name=tool_name)

            if pre_hooks or post_hooks:
                normalized_res["_hooks"] = {"pre": pre_hooks, "post": post_hooks}
            return await self._finalize_workspace_transaction(
                normalized_res,
                store=transaction_store,
                handle=transaction_handle,
                tool_name=tool_name,
            )
        except Exception as e:
            failed = normalize_tool_result(
                {
                    "success": False,
                    "error": str(e),
                    "error_code": ToolErrorCode.TOOL_EXCEPTION,
                },
                tool_name=pc.get("tool_name", "unknown"),
            )
            transaction_store = locals().get("transaction_store")
            transaction_handle = locals().get("transaction_handle")
            return await self._finalize_workspace_transaction(
                failed,
                store=transaction_store,
                handle=transaction_handle,
                tool_name=pc.get("tool_name", "unknown"),
            )
