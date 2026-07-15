"""Tool execution and orchestration for the CoderAI agent.

Handles batch parallelism, read-only vs mutating limits, hook execution,
and UI confirmation.
"""

import asyncio
import json
import logging
import time as _time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.execution_context import (
    execution_context_scope,
    resolve_delegation_isolation_domain,
)
from coderAI.core.loop_guard import (
    DOOM_LOOP_HARD_THRESHOLD as DOOM_LOOP_HARD_THRESHOLD,  # re-exported for tests
    LoopGuard,
    doom_message,
)
from coderAI.core.services import get_services, services_scope
from coderAI.core.tool_routing import (
    call_mcp_tool_by_function_name,
    is_mcp_function_name,
    coerce_tool_arguments,
)
from coderAI.core.permissions import (
    ApprovalRules,
    is_high_risk_no_blanket,
    tool_requires_confirmation,
)
from coderAI.core.provenance import Provenance, wrap_untrusted_output
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.core.tool_results import normalize_tool_result
from coderAI.core.turn import TurnContext
from coderAI.system.error_policy import is_transient_error, is_transient_message
from coderAI.system.locks import canonical_path_key
from coderAI.system.retry import backoff_delay

logger = logging.getLogger(__name__)

# Cap concurrent read-only tools to avoid OS resource exhaustion
MAX_CONCURRENT_READ_ONLY = 20

DEFAULT_TOOL_TIMEOUT_SECONDS = 120.0

# Ceiling on the exponential backoff between transient-failure tool retries.
TOOL_RETRY_DELAY_CAP_SECONDS = 10.0


def resolve_tool_timeout(tool: Any, tool_name: str, arguments: Any) -> float:
    """Effective outer wall-clock cap for one tool call.

    Precedence (first hit wins):

    1. ``tool.resolve_timeout(arguments)`` — argument-derived cap (a tool with
       its own ``timeout`` argument returns it clamped + margin, so the outer
       ``wait_for`` can't fire before the tool's own subprocess cleanup);
    2. ``config.tool_timeout_overrides[tool_name]`` — per-tool config override;
    3. ``tool.timeout`` class attribute;
    4. ``config.tool_timeout_seconds`` — only when explicitly set (config
       file / env / project overlay), so the pydantic default doesn't shadow
       the monkeypatchable module default below;
    5. ``DEFAULT_TOOL_TIMEOUT_SECONDS`` (read live so tests can patch it).

    All access is defensive (``getattr`` / try-except): tests exercise the
    executor with ``SimpleNamespace`` tools and mock agents, and a broken
    ``resolve_timeout`` must degrade to the next level, never sink the call.
    """
    resolver = getattr(tool, "resolve_timeout", None)
    if callable(resolver):
        try:
            resolved = resolver(arguments if isinstance(arguments, dict) else {})
            if resolved is not None:
                return float(resolved)
        except Exception:
            logger.debug("resolve_timeout failed for %s; falling back", tool_name, exc_info=True)

    config: Any = None
    try:
        config = get_services().config
    except Exception:
        config = None

    if config is not None:
        overrides = getattr(config, "tool_timeout_overrides", None)
        if isinstance(overrides, dict):
            override = overrides.get(tool_name)
            if override is not None:
                try:
                    return float(override)
                except (TypeError, ValueError):
                    pass

    tool_timeout = getattr(tool, "timeout", None)
    if tool_timeout:
        try:
            return float(tool_timeout)
        except (TypeError, ValueError):
            pass

    if config is not None and "tool_timeout_seconds" in getattr(config, "model_fields_set", ()):
        try:
            value = float(config.tool_timeout_seconds)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass

    return DEFAULT_TOOL_TIMEOUT_SECONDS


# Cap concurrent read-only sub-agent delegations. Each sub-agent is a full
# LLM session with its own tool loop, so we fan out far less aggressively
# than for cheap read-only tools like read_file / grep.
MAX_CONCURRENT_READ_ONLY_SUBAGENTS = 4

DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS = 3

# Maximum number of entries in the preview file cache. Beyond this limit, the
# least-recently-used entry is evicted to bound memory usage.
PREVIEW_FILE_CACHE_MAX_ENTRIES = 50

# Maximum combined size (bytes) of cached file contents. When exceeded, LRU
# entries are dropped until the total is within the limit.
PREVIEW_FILE_CACHE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


class BatchStatus(Enum):
    """Outcome of running one batch of tool calls (Phase 2.1).

    Replaces the old ``Tuple[bool, Optional[Dict]]`` with sentinel keys
    (``{"retry": True}`` / ``{"_denied": ...}`` / ``{"_doom_loop_stop": ...}``)
    that ``ExecutionLoop`` had to reverse-engineer with an if-cascade.
    """

    OK = "ok"  # tools ran; at least one succeeded — continue the loop.
    RETRY = "retry"  # all failed (or unparsable) — feed errors back to the LLM.
    DENIED = "denied"  # one or more tools were denied by the user.
    DOOM_LOOP = "doom_loop"  # identical call repeated past the hard threshold.


@dataclass
class ToolBatchOutcome:
    """Typed result of :meth:`ToolExecutor.orchestrate_tool_calls`."""

    status: BatchStatus
    denied_tools: List[str] = field(default_factory=list)
    doom_tool: Optional[str] = None
    doom_count: int = 0


def _extract_vision_images(
    res: Any,
) -> Tuple[Any, Optional[List[Dict[str, Any]]]]:
    """Split a vision tool result into a lightweight text dict + image blocks.

    Tools like ``read_image`` return ``{"_vision": True, "image_data": <b64>,
    "mime_type": ...}``. The base64 payload must NOT go through result
    summarization (it would be truncated and corrupted) or be stringified into
    the text content (huge + useless to the model). This pulls the image out so
    it can be carried as a structured ``tool_images`` block, leaving a small
    text dict behind. Returns ``(clean_result, images)`` where ``images`` is
    ``None`` when the result carries no usable image.
    """
    if not isinstance(res, dict) or not res.get("_vision"):
        return res, None
    data = res.get("image_data")
    mime = res.get("mime_type")
    if not (isinstance(data, str) and data and isinstance(mime, str) and mime):
        return res, None
    images = [{"mime_type": mime, "data": data}]
    clean = {k: v for k, v in res.items() if k != "image_data"}
    clean["image_attached"] = True
    return clean, images


class ToolExecutor:
    agent: Any
    loop_guard: LoopGuard
    _turn: TurnContext
    _ro_semaphore: asyncio.Semaphore
    _subagent_ro_semaphore: asyncio.Semaphore
    _subagent_mut_semaphore: asyncio.Semaphore
    _confirm_lock: asyncio.Lock
    _preview_file_cache: "OrderedDict[str, Tuple[float, str]]"

    def __init__(self, agent: Any, loop_guard: Optional[LoopGuard] = None) -> None:
        self.agent = agent
        # Per-turn state shared with ``ExecutionLoop`` (Phase 4.1). ``run()``
        # passes its ``TurnContext`` into ``orchestrate_tool_calls``; a standalone
        # executor (tests) keeps this default so the egress-gate taint persists
        # across successive batches on the same instance.
        self._turn = TurnContext()
        # One guard per turn owns fingerprinting, repeat counters, cached-repeat
        # decisions, and the doom-loop thresholds (Phase 2.2). ``ExecutionLoop``
        # creates it and shares the same instance so the in-batch and
        # cross-iteration paths agree. A standalone executor (tests) gets its own.
        self.loop_guard = loop_guard if loop_guard is not None else LoopGuard()
        self._ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY)
        self._subagent_ro_semaphore = asyncio.Semaphore(MAX_CONCURRENT_READ_ONLY_SUBAGENTS)
        mut_cap = self._mutating_subagent_cap()
        self._subagent_mut_semaphore = asyncio.Semaphore(mut_cap)
        self._confirm_lock = asyncio.Lock()
        self._preview_file_cache: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        # Once a mutation has run in this turn, earlier cached reads may be
        # stale. Consecutive reads can still dedupe within their current phase.
        self._mutation_seen = False

    def _mutating_subagent_cap(self) -> int:
        cfg = getattr(self.agent, "config", None)
        try:
            cap = int(
                getattr(
                    cfg,
                    "max_concurrent_mutating_subagents",
                    DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS,
                )
            )
            return max(1, min(8, cap))
        except (TypeError, ValueError):
            return DEFAULT_MAX_CONCURRENT_MUTATING_SUBAGENTS

    def _cache_preview(self, path: str, mtime: float, content: str) -> None:
        self._preview_file_cache[path] = (mtime, content)
        self._preview_file_cache.move_to_end(path)
        while (
            len(self._preview_file_cache) > PREVIEW_FILE_CACHE_MAX_ENTRIES
            or sum(len(v[1]) for v in self._preview_file_cache.values())
            > PREVIEW_FILE_CACHE_MAX_BYTES
        ):
            self._preview_file_cache.popitem(last=False)

    def _is_call_preapproved(self, tool_name: str, arguments: Optional[Dict[str, Any]]) -> bool:
        """True if this exact call is covered by an "always allow" rule (Phase 4.2).

        The real agent carries an :class:`ApprovalRules`, which scopes high-risk
        tools to a reviewed command-prefix / path (a bare-name allow of
        ``run_command`` never authorizes a *different* command). A plain set of
        names is still accepted as a legacy/test shim, but only for tools that
        are not high-risk (see :func:`is_high_risk_no_blanket`).
        """
        rules = getattr(self.agent, "_tool_approval_allowlist", None)
        if rules is None:
            return False
        if isinstance(rules, ApprovalRules):
            return rules.is_allowed(tool_name, arguments)
        try:
            name_allowed = tool_name in rules
        except TypeError:
            return False
        return bool(name_allowed) and not is_high_risk_no_blanket(tool_name)

    def _result_provenance(self, tool_name: str) -> str:
        """Taint label for *tool_name*'s results (Phase 3.1).

        Real tools declare ``result_provenance``; MCP proxy calls (no local Tool
        object) are always ``UNTRUSTED_EXTERNAL`` — a third-party server's output
        must never carry system authority (confused-deputy, Phase 7.3).
        """
        tool = self.agent.tools.get(tool_name)
        if tool is not None:
            return str(getattr(tool, "result_provenance", Provenance.TRUSTED))
        if is_mcp_function_name(tool_name):
            return Provenance.UNTRUSTED_EXTERNAL
        return Provenance.TRUSTED

    def _mark_turn_untrusted(self, *, from_mcp: bool = False) -> None:
        """Record that this user turn has ingested untrusted external content.

        Arms the egress gate (:meth:`_turn_has_untrusted`). When the content came
        from an MCP server, also arms the stronger mutating-local gate
        (:meth:`_turn_has_untrusted_mcp`, Phase 7.3). The taint lives on the shared
        :class:`TurnContext`, which is fresh per user message.
        """
        self._turn.ingested_untrusted = True
        if from_mcp:
            self._turn.ingested_untrusted_mcp = True

    def _turn_has_untrusted(self) -> bool:
        return self._turn.ingested_untrusted

    def _turn_has_untrusted_mcp(self) -> bool:
        return self._turn.ingested_untrusted_mcp

    @staticmethod
    def _untrusted_source(pc: Dict[str, Any]) -> str:
        """Short ``source`` label for the untrusted-output fence.

        Tool name, plus the fetch target (url/query) when available so a reviewer
        can see where the content came from. Sanitized by ``wrap_untrusted_output``.
        """
        name = pc.get("tool_name", "unknown")
        args = pc.get("arguments") or {}
        target = None
        if isinstance(args, dict):
            target = args.get("url") or args.get("query")
        if isinstance(target, str) and target.strip():
            return f"{name}:{target}"
        return str(name)

    @staticmethod
    def _dedupe_safe(tool: Any) -> bool:
        """Whether an identical call may reuse a prior result."""
        if tool is None:
            return False
        declared = getattr(tool, "dedupe_safe", None)
        if declared is not None:
            return bool(declared)
        return bool(getattr(tool, "is_read_only", False))

    @staticmethod
    def _idempotent(tool: Any) -> bool:
        """Whether retrying an identical call is safe."""
        if tool is None:
            return False
        declared = getattr(tool, "idempotent", None)
        if declared is not None:
            return bool(declared)
        return bool(getattr(tool, "is_read_only", False))

    def _enter_waiting_for_user(
        self, tool_name: str
    ) -> Optional[Tuple[AgentStatus, Optional[str]]]:
        info = self.agent.tracker_info
        if not info:
            return None
        previous = (info.status, info.current_tool)
        self.agent.tracker_update(status=AgentStatus.WAITING_FOR_USER, current_tool=tool_name)
        return previous

    def _exit_waiting_for_user(self, previous: Optional[Tuple[AgentStatus, Optional[str]]]) -> None:
        info = self.agent.tracker_info
        if not info or previous is None:
            return
        if info.status == AgentStatus.CANCELLED:
            self.agent.tracker_update()
            return
        prev_status, prev_tool = previous
        self.agent.tracker_update(status=prev_status, current_tool=prev_tool)

    @property
    def _read_only_semaphore(self) -> asyncio.Semaphore:
        return self._ro_semaphore

    @property
    def _read_only_subagent_semaphore(self) -> asyncio.Semaphore:
        return self._subagent_ro_semaphore

    @staticmethod
    def _truncate_preview(text: str) -> str:
        """Cap an approval preview at 32KB with a visible truncation marker."""
        if len(text) > 32768:
            hidden = len(text) - 32768
            return text[:32768] + f"\n... (diff truncated) {hidden} chars hidden"
        return text

    def _compute_preview_diff(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        """Render an approval diff for a file-editing call (Phase 4.3).

        The editing *semantics* live on the tool (:meth:`Tool.preview`); this
        method owns only the trust-boundary plumbing: project-scope check, the
        mtime-keyed original-content cache, unified-diff rendering, and 32KB
        truncation. A tool either returns the new file content (rendered here as
        a diff) or a pre-rendered diff shown verbatim.
        """
        tools = getattr(self.agent, "tools", None)
        tool = tools.get(tool_name) if tools is not None else None
        if tool is None:
            return None

        path = arguments.get("path")
        if not path:
            return None

        from pathlib import Path
        import difflib

        try:
            path_obj = Path(path).expanduser().resolve()

            from coderAI.tools.filesystem import _allows_outside_project

            if self.agent and self.agent.config and not _allows_outside_project():
                project_root = Path(self.agent.config.project_root).resolve()
                try:
                    path_obj.relative_to(project_root)
                except ValueError:
                    return None

            # Read the current file text (None when it doesn't exist yet) via the
            # mtime cache so repeated previews don't re-read unchanged files.
            original: Optional[str] = None
            if path_obj.exists():
                try:
                    resolved = str(path_obj.resolve())
                    current_mtime = path_obj.stat().st_mtime
                    cached = self._preview_file_cache.get(resolved)
                    if cached is not None and cached[0] == current_mtime:
                        self._preview_file_cache.move_to_end(resolved)
                        original = cached[1]
                    else:
                        original = path_obj.read_text(encoding="utf-8")
                        self._cache_preview(resolved, current_mtime, original)
                except Exception:
                    return None

            preview = tool.preview(arguments, original)
            if preview is None:
                return None
            if preview.rendered_diff is not None:
                return self._truncate_preview(preview.rendered_diff)
            if preview.new_content is None:
                return None

            original_text = original or ""
            diff_lines = list(
                difflib.unified_diff(
                    original_text.splitlines(keepends=True),
                    preview.new_content.splitlines(keepends=True),
                    fromfile=f"a/{path_obj.name}",
                    tofile=f"b/{path_obj.name}",
                    n=3,
                )
            )
            return self._truncate_preview("".join(diff_lines))
        except Exception as e:
            logger.debug("Preview diff computation failed for %s: %s", tool_name, e)
            return None

    async def _precompute_diffs(self, parsed_calls: list) -> Dict[int, Optional[str]]:
        gated: List[Tuple[int, dict]] = []
        for i, pc in enumerate(parsed_calls):
            if pc.get("parse_error") or pc.get("arguments") is None:
                continue
            tool = self.agent.tools.get(pc.get("tool_name", ""))
            if tool is not None and tool_requires_confirmation(tool):
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
        *,
        force_confirm: bool = False,
    ) -> bool:
        # Headless / non-interactive override (e.g. `coderAI run`): when set,
        # decide here instead of prompting. A forced MCP-tainted mutation may
        # be denied by an override, but an override allow is not a human
        # confirmation and therefore falls through to the real approval path.
        if bool(getattr(self.agent, "auto_approve", False)) and not force_confirm:
            return True

        override = getattr(self.agent, "confirmation_override", None)
        if override is not None:
            override_allowed = bool(await override(tool_name, arguments))
            if not override_allowed or not force_confirm:
                return override_allowed

        # getattr avoids a hard import on ipc_server; the agent may run
        # without IPC (e.g. one-shot CLI) where ipc_server is never set.
        ipc_server = getattr(self.agent, "ipc_server", None)

        if force_confirm and ipc_server is None:
            import sys

            if not sys.stdin.isatty():
                logger.warning(
                    "Forced human approval unavailable for MCP-tainted mutation '%s'; denying.",
                    tool_name,
                )
                return False

        diff = (
            precomputed_diff
            if precomputed_diff is not None
            else await asyncio.to_thread(self._compute_preview_diff, tool_name, arguments)
        )

        async with self._confirm_lock:
            # Always (a) may have enabled YOLO while this call was queued
            # behind another approval — honour it without a second prompt.
            if bool(getattr(self.agent, "auto_approve", False)) and not force_confirm:
                return True
            if ipc_server is None:
                args_preview = json.dumps(arguments, indent=2)
                if len(args_preview) > 300:
                    args_preview = args_preview[:300] + "\n  ... (truncated)"

                diff_preview = f"\n\nDiff Preview:\n{diff}" if diff else ""

                get_services().events.emit(
                    "agent_status",
                    message=(
                        f"\n⚠ Tool '{tool_name}' requires confirmation."
                        f"\n{args_preview}"
                        f"{diff_preview}"
                    ),
                )

            previous = self._enter_waiting_for_user(tool_name)
            try:
                if ipc_server is not None:
                    timeout_s = int(
                        getattr(self.agent.config, "approval_timeout_seconds", 300) or 0
                    )
                    approval_coro = ipc_server.request_tool_approval(
                        tool_id=tool_id or str(uuid.uuid4()),
                        tool_name=tool_name,
                        arguments=arguments,
                        diff=diff,
                    )
                    try:
                        if timeout_s > 0:
                            res = await asyncio.wait_for(approval_coro, timeout=timeout_s)
                        else:
                            res = await approval_coro
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Tool approval timed out after %ss for '%s' — auto-denying.",
                            timeout_s,
                            tool_name,
                        )
                        return False
                    return bool(res)

                try:
                    from prompt_toolkit import PromptSession

                    prompt_session: Any = PromptSession()
                    answer = await prompt_session.prompt_async("Allow this tool? (y/n) > ")
                except (ImportError, EOFError, KeyboardInterrupt):
                    try:
                        loop = asyncio.get_running_loop()
                        answer = await loop.run_in_executor(
                            None, lambda: input("Allow this tool? (y/n) > ")
                        )
                    except (EOFError, KeyboardInterrupt):
                        answer = "n"

                return answer.strip().lower() in ("y", "yes")
            finally:
                self._exit_waiting_for_user(previous)

    async def execute_single_tool(
        self,
        pc: Dict[str, Any],
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager: Any,
        precomputed_diff: Optional[str] = None,
    ) -> Dict[str, Any]:
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

            with execution_context_scope(agent_id, isolation_domain=isolation_domain):
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
        pc: Dict[str, Any],
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager: Any,
        *,
        precomputed_diff: Optional[str] = None,
        tool: Any = None,
        tool_name: str = "",
        arguments: Any = None,
    ) -> Dict[str, Any]:
        try:
            is_mcp_proxy = is_mcp_function_name(tool_name) and tool is None
            allowed_native = vars(self.agent).get("_allowed_native_tool_names")
            if allowed_native is not None and (
                is_mcp_proxy or tool_name not in allowed_native or tool is None
            ):
                return normalize_tool_result(
                    {
                        "success": False,
                        "error": (
                            f"Tool '{tool_name}' is outside this sub-agent's "
                            f"{vars(self.agent).get('_capability_domain', 'restricted')} "
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
                not self.agent.auto_approve
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
                not self.agent.auto_approve
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

            async def _confirm(name: str, args: Dict[str, Any]) -> bool:
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

            pre_hooks = (
                await hooks_manager.run_hooks(tool_name, "PreToolUse", arguments, hooks_data) or []
            )
            for hook_msg in pre_hooks:
                if hook_msg.startswith("[PreToolUse Hook ERROR]"):
                    return {
                        "success": False,
                        "error": hook_msg,
                        "error_code": ToolErrorCode.HOOK_BLOCKED,
                    }

            timeout = resolve_tool_timeout(tool, tool_name, arguments)

            async def _inner_execute() -> Any:
                if is_mcp_proxy:
                    return await call_mcp_tool_by_function_name(tool_name, arguments)
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

            async def _retry_pause(attempt: int, why: str) -> None:
                delay = backoff_delay(
                    attempt, base=retry_base_delay, cap=TOOL_RETRY_DELAY_CAP_SECONDS
                )
                message = (
                    f"Tool '{tool_name}' hit a transient failure "
                    f"(attempt {attempt}/{attempts_allowed}) — retrying in {delay:.1f}s: {why}"
                )
                logger.warning(message)
                get_services().events.emit("agent_warning", message=message)
                await asyncio.sleep(delay)

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
                        await _retry_pause(attempt, str(e))
                        continue
                    raise
                if (
                    attempt < attempts_allowed
                    and isinstance(result, dict)
                    and result.get("success") is False
                    and not _cancelled()
                    and is_transient_message(str(result.get("error") or ""))
                ):
                    await _retry_pause(attempt, str(result.get("error") or ""))
                    continue
                break

            post_hook_args = dict(arguments or {})
            if tool_timed_out:
                post_hook_args["_tool_timed_out"] = True
            post_hooks = (
                await hooks_manager.run_hooks(tool_name, "PostToolUse", post_hook_args, hooks_data)
                or []
            )
            normalized_res: Dict[str, Any] = normalize_tool_result(result, tool_name=tool_name)

            if pre_hooks or post_hooks:
                normalized_res["_hooks"] = {"pre": pre_hooks, "post": post_hooks}
            return normalized_res
        except Exception as e:
            return normalize_tool_result(
                {
                    "success": False,
                    "error": str(e),
                    "error_code": ToolErrorCode.TOOL_EXCEPTION,
                },
                tool_name=pc.get("tool_name", "unknown"),
            )

    async def orchestrate_tool_calls(
        self,
        tool_calls: list,
        messages: List[Dict[str, Any]],
        user_message: str,
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager: Any,
        turn: Optional[TurnContext] = None,
    ) -> ToolBatchOutcome:
        # Adopt the loop-owned per-turn state (Phase 4.1) so the egress-gate
        # taint and reply state live in one object. A direct/test call without a
        # turn keeps the executor's own default ``TurnContext``.
        if turn is not None:
            if turn is not self._turn:
                self._mutation_seen = False
            self._turn = turn
        # Bind the owning agent's effective config (project overrides included)
        # for the duration of the batch. Stores still resolve to the shared
        # process-wide instances through the parent chain, so cross-agent
        # sharing (notepad/tracker/undo) is unchanged.
        with services_scope(inherit=True, config=getattr(self.agent, "config", None)):
            return await self._orchestrate_tool_calls(
                tool_calls, messages, user_message, hooks_data, hooks_manager
            )

    async def _orchestrate_tool_calls(
        self,
        tool_calls: list,
        messages: List[Dict[str, Any]],
        user_message: str,
        hooks_data: Optional[Dict[str, Any]],
        hooks_manager: Any,
    ) -> ToolBatchOutcome:
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
                parsed_calls.append(
                    {
                        "tool_id": tool_id,
                        "tool_name": name,
                        "arguments": None,
                        "parse_error": arg_err,
                    }
                )
            else:
                parsed_calls.append(
                    {"tool_id": tool_id, "tool_name": name, "arguments": args, "parse_error": None}
                )

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
                            "error_code": ToolErrorCode.PARSE_ERROR,
                        }
                    ),
                    tool_call_id=pc["tool_id"],
                    name=pc["tool_name"],
                )

            messages.clear()
            messages.extend(self.agent.session.get_messages_for_api())
            return ToolBatchOutcome(BatchStatus.RETRY)

        if self.agent.tracker_info:
            self.agent.tracker_update(
                status=AgentStatus.TOOL_CALL,
                current_tool=", ".join(pc["tool_name"] for pc in parsed_calls if pc["arguments"]),
            )

        for pc in parsed_calls:
            if pc["parse_error"] is not None:
                get_services().events.emit(
                    "tool_error", tool_name=pc["tool_name"], error=pc["parse_error"]
                )
            elif pc["arguments"] is not None:
                get_services().events.emit(
                    "tool_call",
                    tool_name=pc["tool_name"],
                    arguments=pc["arguments"],
                    tool_id=pc["tool_id"],
                )

        dup_results: Dict[int, Dict[str, Any]] = {}
        batch_seen: Dict[str, int] = {}
        to_run_indices: List[int] = []
        mutation_before = self._mutation_seen
        for idx, pc in enumerate(parsed_calls):
            if pc["parse_error"] is not None or pc["arguments"] is None:
                to_run_indices.append(idx)
                continue
            fp = self.loop_guard.fingerprint(pc["tool_name"], pc["arguments"])
            pc["_fp"] = fp
            tool = self.agent.tools.get(pc["tool_name"])
            dedupe_safe = self._dedupe_safe(tool)
            is_read_only = bool(tool and getattr(tool, "is_read_only", False))
            if not is_read_only:
                # A read result cannot be reused across a mutation barrier.
                batch_seen.clear()

            if dedupe_safe and fp in batch_seen:
                dup_results[idx] = {
                    "_dup_of_batch_index": batch_seen[fp],
                    "_warning": (
                        f"Duplicate call to '{pc['tool_name']}' in the same batch — "
                        "result reused from the first call. Avoid emitting identical "
                        "parallel tool calls."
                    ),
                }
                continue

            prior_count = self.loop_guard.prior_count(fp)
            repeat = self.loop_guard.cached_repeat(
                pc["tool_name"], dedupe_safe and not mutation_before, fp
            )
            if repeat is not None:
                cached, repeated_count = repeat
                pc["_cached_repeat_count"] = repeated_count
                if pc["tool_name"] == "delegate_task":
                    cached["_warning"] = (
                        f"This is call #{repeated_count} to 'delegate_task' with identical "
                        "arguments — returning the cached report. Do not re-delegate the same task."
                    )
                else:
                    cached["_warning"] = (
                        f"This is call #{repeated_count} to '{pc['tool_name']}' with identical "
                        "arguments — returning the cached result. Stop repeating the same read; "
                        "either work with the data you already have or try a different approach."
                    )
                dup_results[idx] = cached
                get_services().events.emit(
                    "agent_warning",
                    message=(
                        f"Skipping duplicate delegate_task (already run {prior_count}×)."
                        if pc["tool_name"] == "delegate_task"
                        else f"Skipping duplicate read-only call to {pc['tool_name']} (already run {prior_count}×)."
                    ),
                )
                continue

            if dedupe_safe:
                batch_seen[fp] = idx
            to_run_indices.append(idx)
            if not is_read_only:
                mutation_before = True
                self._mutation_seen = True

        calls_to_run = [parsed_calls[i] for i in to_run_indices]
        run_results = await self.run_tool_batch(calls_to_run, hooks_data, hooks_manager)

        # Merge real results + dup short-circuit results back into original order
        results: List[Any] = [None] * len(parsed_calls)
        for i, r in zip(to_run_indices, run_results):
            results[i] = r
        for i, placeholder in dup_results.items():
            src = placeholder.pop("_dup_of_batch_index", None)
            if src is not None and results[src] is not None:
                cloned = (
                    dict(results[src])
                    if isinstance(results[src], dict)
                    else {"output": results[src]}
                )
                cloned["_warning"] = placeholder.get("_warning", "Duplicate result reused.")
                results[i] = cloned
            else:
                placeholder["error"] = "Duplicate tool call skipped"
                results[i] = placeholder

        # Update call counters / last-result cache for future iterations via the
        # shared LoopGuard, and detect cross-iteration doom-loops here: if any
        # fingerprint has now been called past its hard threshold we signal the
        # loop to terminate after persisting the current results.
        doom_offender: Optional[Tuple[str, int]] = None  # (tool_name, count)
        executed_indices = set(to_run_indices)
        for idx, (pc, res) in enumerate(zip(parsed_calls, results)):
            fp_val = pc.get("_fp")
            if not fp_val or not isinstance(fp_val, str):
                continue
            fp = fp_val
            if not fp:
                continue
            if idx not in executed_indices:
                continue
            # User-denied calls don't reflect a stuck model — the user can
            # deny the same write 5× because they're reviewing each preview.
            # Treating denials as doom-loop hits produces a misleading
            # "stuck in a loop" stop instead of a clean "you keep denying".
            if isinstance(res, dict) and res.get("error_code") == ToolErrorCode.DENIED:
                continue
            count = self.loop_guard.record_execution(fp, res)
            if self.loop_guard.is_doom(pc["tool_name"], count) and (
                doom_offender is None or count > doom_offender[1]
            ):
                doom_offender = (pc["tool_name"], count)

        for pc in parsed_calls:
            cached_count = pc.get("_cached_repeat_count")
            if (
                isinstance(cached_count, int)
                and self.loop_guard.is_doom(pc["tool_name"], cached_count)
                and (doom_offender is None or cached_count > doom_offender[1])
            ):
                doom_offender = (pc["tool_name"], cached_count)

        for pc, res in zip(parsed_calls, results):
            # Pull any base64 image out BEFORE summarization so it reaches the
            # model as a real vision block instead of being truncated/stringified.
            res, images = _extract_vision_images(res)
            res = self.agent.context_controller.summarize_tool_result(res)
            get_services().events.emit(
                "tool_result", tool_name=pc["tool_name"], result=res, tool_id=pc["tool_id"]
            )
            extra: Dict[str, Any] = {"name": pc["tool_name"]}
            if images:
                extra["tool_images"] = images

            # Provenance (Phase 3.2): tool results that ingest outside data are
            # serialized inside a non-authoritative <untrusted_tool_output> block
            # and mark the turn as tainted so the egress gate (3.4) arms. The UI
            # event above still carries the clean dict — only the model-facing
            # transcript is fenced.
            serialized = json.dumps(res)
            if self._result_provenance(pc["tool_name"]) == Provenance.UNTRUSTED_EXTERNAL:
                # A static mcp_* tool (mcp_call_tool, mcp_read_resource, …) relays
                # third-party server output but has a local Tool object, so the
                # name-based proxy check misses it — self-declared mcp_source is
                # what arms the confused-deputy (MCP-mutation) gate for it.
                tool_obj = self.agent.tools.get(pc["tool_name"])
                from_mcp = is_mcp_function_name(pc["tool_name"]) or bool(
                    getattr(tool_obj, "mcp_source", False)
                )
                self._mark_turn_untrusted(from_mcp=from_mcp)
                serialized = wrap_untrusted_output(serialized, self._untrusted_source(pc))
            self.agent.session.add_message("tool", serialized, tool_call_id=pc["tool_id"], **extra)

        if self.agent.tracker_info:
            self.agent.tracker_update(current_tool=None)

        # Update the messages list from session
        messages.clear()
        messages.extend(self.agent.session.get_messages_for_api())

        # Detect which failures are user denials (not real errors).
        denied_tools: List[str] = []
        for pc, res in zip(parsed_calls, results):
            if isinstance(res, dict) and res.get("error_code") == ToolErrorCode.DENIED:
                denied_tools.append(pc.get("tool_name", "unknown"))

        all_tool_calls_failed = bool(results) and all(
            not (isinstance(res, dict) and res.get("success") is True) for res in results
        )
        if all_tool_calls_failed:
            if denied_tools:
                get_services().events.emit(
                    "agent_warning",
                    message=f"Tool(s) denied by user: {', '.join(denied_tools)}. "
                    "Asking the model to try a different approach.",
                )
                return ToolBatchOutcome(BatchStatus.DENIED, denied_tools=denied_tools)
            get_services().events.emit(
                "agent_warning",
                message="All tool calls in this step failed. Asking the model to revise its plan.",
            )
            return ToolBatchOutcome(BatchStatus.RETRY)

        if denied_tools:
            return ToolBatchOutcome(BatchStatus.DENIED, denied_tools=denied_tools)

        if doom_offender is not None:
            tool_name, count = doom_offender
            get_services().events.emit("agent_warning", message=doom_message(tool_name, count))
            return ToolBatchOutcome(BatchStatus.DOOM_LOOP, doom_tool=tool_name, doom_count=count)

        return ToolBatchOutcome(BatchStatus.OK)

    async def run_tool_batch(
        self, parsed_calls: list, hooks_data: Optional[Dict[str, Any]], hooks_manager: Any
    ) -> list:
        results: List[Any] = [None] * len(parsed_calls)
        total, done = len(parsed_calls), 0
        # _cancel_event is an asyncio.Event on AgentTrackerInfo used to
        # signal cancellation across concurrent tool tasks.
        cancel_event = self.agent.tracker_info._cancel_event if self.agent.tracker_info else None

        precomputed_diffs = await self._precompute_diffs(parsed_calls)

        def _is_read_call(pc: Dict[str, Any]) -> bool:
            if pc.get("tool_name") == "delegate_task" and isinstance(pc.get("arguments"), dict):
                return resolve_delegation_isolation_domain(pc["arguments"]) == "read_only"
            tool = self.agent.tools.get(pc.get("tool_name", ""))
            return bool(tool and getattr(tool, "is_read_only", False))

        def _cancelled_result() -> Dict[str, Any]:
            return {
                "success": False,
                "error": "Cancelled by user.",
                "error_code": ToolErrorCode.CANCELLED,
            }

        async def _run(pc: Dict[str, Any], diff: Optional[str] = None) -> Dict[str, Any]:
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

        def _coerce_gather_result(idx: int, raw: Any) -> Dict[str, Any]:
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

        def _serializes_by_path(tool_name: str) -> bool:
            tool = self.agent.tools.get(tool_name)
            return bool(tool and getattr(tool, "batch_serialize_by_path", False))

        def _path_for(pc: Dict[str, Any]) -> Optional[str]:
            args = pc.get("arguments") or {}
            path = args.get("path") or args.get("file_path")
            return path if isinstance(path, str) and path else None

        def _phase_kind(pc: Dict[str, Any]) -> str:
            if _is_read_call(pc):
                return "read"
            if pc.get("tool_name") == "delegate_task" and isinstance(pc.get("arguments"), dict):
                if resolve_delegation_isolation_domain(pc["arguments"]) == "browser":
                    return "browser"
            if _serializes_by_path(pc.get("tool_name", "")) and _path_for(pc) is not None:
                return "path"
            return "mutation"

        async def _run_read(idx: int, caps: Dict[str, asyncio.Semaphore]) -> Dict[str, Any]:
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

        async def _run_browser(idx: int) -> Dict[str, Any]:
            async with self._subagent_mut_semaphore:
                return await _run(parsed_calls[idx], diff=None)

        mutation_completed = False
        cursor = 0
        while cursor < len(parsed_calls):
            kind = _phase_kind(parsed_calls[cursor])
            if kind in {"read", "browser", "path"}:
                phase_indices: List[int] = []
                while cursor < len(parsed_calls) and _phase_kind(parsed_calls[cursor]) == kind:
                    phase_indices.append(cursor)
                    cursor += 1

                if kind == "read":
                    caps: Dict[str, asyncio.Semaphore] = {}
                    raw_results = await asyncio.gather(
                        *(_run_read(idx, caps) for idx in phase_indices),
                        return_exceptions=True,
                    )
                    for idx, raw in zip(phase_indices, raw_results):
                        results[idx] = _coerce_gather_result(idx, raw)
                        _emit_progress(idx)
                    continue

                if kind == "browser":
                    raw_results = await asyncio.gather(
                        *(_run_browser(idx) for idx in phase_indices),
                        return_exceptions=True,
                    )
                    for idx, raw in zip(phase_indices, raw_results):
                        results[idx] = _coerce_gather_result(idx, raw)
                        _emit_progress(idx)
                    mutation_completed = True
                    continue

                path_queues: Dict[str, List[int]] = {}
                for idx in phase_indices:
                    path = _path_for(parsed_calls[idx])
                    assert path is not None
                    path_queues.setdefault(canonical_path_key(path), []).append(idx)

                async def _run_path_queue(path_indices: List[int]) -> None:
                    for pos, idx in enumerate(path_indices):
                        t0 = _time.time()
                        diff = (
                            precomputed_diffs.get(idx)
                            if not mutation_completed and pos == 0
                            else None
                        )
                        results[idx] = await _run(parsed_calls[idx], diff=diff)
                        _emit_progress(idx, elapsed=round(_time.time() - t0, 2))

                await asyncio.gather(
                    *(_run_path_queue(indices) for indices in path_queues.values())
                )
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
