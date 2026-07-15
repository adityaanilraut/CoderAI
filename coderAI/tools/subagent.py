"""Sub-agent delegation tool for multi-agent capabilities."""

import asyncio
import logging
import os
import time as _time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.tools.base import Tool
from coderAI.core.agent_tracker import AgentStatus
from coderAI.core.execution_context import resolve_delegation_isolation_domain
from coderAI.system.error_policy import BudgetExceededError, is_transient_error
from coderAI.system.events import event_emitter
from coderAI.system.retry import backoff_delay

logger = logging.getLogger(__name__)

# Maximum depth for nested sub-agent delegation to prevent infinite recursion
MAX_DELEGATION_DEPTH = 3

# Transient-failure retries for one delegation (documented "retried up to 2
# times with exponential backoff"). Retries re-run on the SAME sub-agent /
# session — cheaper than a fresh spawn and it preserves task_id resume.
MAX_DELEGATION_RETRIES = 2
DELEGATION_RETRY_DELAY_CAP_SECONDS = 30.0

# Sub-agent domains are capability boundaries, not scheduling hints. These are
# exact native-tool allowlists so a newly registered tool fails closed until it
# is deliberately assigned to a domain.
READ_ONLY_NATIVE_CAPABILITIES = frozenset(
    {
        "file_readlink",
        "file_stat",
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep",
        "list_directory",
        "list_processes",
        "read_bg_output",
        "read_file",
        "read_image",
        "read_url",
        "recall_memory",
        "semantic_search",
        "symbol_search",
        "undo_history",
        "use_skill",
        "web_search",
    }
)

BROWSER_NATIVE_CAPABILITIES = READ_ONLY_NATIVE_CAPABILITIES | frozenset(
    {
        "browser_click",
        "browser_close",
        "browser_evaluate",
        "browser_get_content",
        "browser_navigate",
        "browser_screenshot",
        "browser_select_option",
        "browser_snapshot",
        "browser_type",
        "browser_wait",
    }
)

DESKTOP_NATIVE_CAPABILITIES = READ_ONLY_NATIVE_CAPABILITIES | frozenset(
    {
        "click_ui_element",
        "get_accessibility_tree",
        "run_applescript",
        "type_keystrokes",
    }
)

WORKSPACE_NATIVE_CAPABILITIES = frozenset(
    {
        "apply_diff",
        "copy_file",
        "create_directory",
        "delegate_task",
        "delete_file",
        "delete_memory",
        "download_file",
        "file_chmod",
        "file_readlink",
        "file_stat",
        "format",
        "git_add",
        "git_branch",
        "git_commit",
        "git_diff",
        "git_log",
        "git_status",
        "glob_search",
        "grep",
        "http_request",
        "kill_process",
        "lint",
        "list_directory",
        "list_processes",
        "manage_context",
        "manage_tasks",
        "move_file",
        "package_manager",
        "python_repl",
        "read_bg_output",
        "read_file",
        "read_image",
        "read_url",
        "recall_memory",
        "refactor",
        "run_background",
        "run_command",
        "run_tests",
        "save_memory",
        "search_replace",
        "semantic_search",
        "symbol_search",
        "undo",
        "undo_history",
        "use_skill",
        "web_search",
        "write_file",
    }
)

NATIVE_CAPABILITY_SETS = {
    "read_only": READ_ONLY_NATIVE_CAPABILITIES,
    "browser": BROWSER_NATIVE_CAPABILITIES,
    "desktop": DESKTOP_NATIVE_CAPABILITIES,
    "workspace": WORKSPACE_NATIVE_CAPABILITIES,
}


@dataclass
class SubagentContext:
    """References from the parent Agent needed by DelegateTaskTool."""

    parent_agent_id: Optional[str] = None
    parent_model: Optional[str] = None
    parent_context_controller: Optional[Any] = None  # ContextController
    parent_cost_tracker: Optional[Any] = None  # CostTracker (shared)
    parent_auto_approve: bool = False
    parent_ipc_server: Optional[Any] = None
    parent_session: Optional[Any] = None  # Session
    delegation_depth: int = 0
    parent_config: Optional[Any] = None  # Config — drives subagent_timeout_seconds
    parent_read_cache: Optional[Any] = None  # FileReadCache
    # Phase 5.1: the parent's registered tool names. A child's tool set is
    # intersected with this so a delegation can never hold a capability the
    # parent lacks (transitivity: child ⊆ parent). None means "unknown" —
    # treated as no ceiling only when the parent context was never wired.
    parent_tool_names: Optional[frozenset] = None
    # Phase 5.2: the parent's confirmation policy (e.g. headless deny-on-mutate),
    # propagated so a child's mutating tools face the same gate and denials land
    # in the same audit list. An async ``(tool_name, arguments) -> bool`` callable.
    parent_confirmation_override: Optional[Any] = None


# Number of recent parent tool calls to summarise for the sub-agent so it
# doesn't repeat inspection work the parent has already done.
RECENT_TOOL_HISTORY_LIMIT = 10


def _summarize_parent_tool_history(
    parent_session: Any, limit: int = RECENT_TOOL_HISTORY_LIMIT
) -> Optional[str]:
    """Build a short markdown summary of the parent's most recent tool calls.

    Returns ``None`` when there is nothing useful to share. Each entry shows
    the tool name only. Arguments, results, and error bodies are deliberately
    excluded because they may contain lower-authority or untrusted text and this
    summary is inserted into the child's system preamble.
    """
    if parent_session is None:
        return None
    messages = getattr(parent_session, "messages", None) or []
    if not messages:
        return None

    # Walk backwards collecting tool names with matching assistant calls.
    tool_names: List[str] = []
    i = len(messages) - 1
    while i >= 0 and len(tool_names) < limit:
        msg = messages[i]
        if getattr(msg, "role", None) == "tool":
            tool_name = getattr(msg, "name", None) or "unknown"
            # Find the preceding assistant message that emitted this tool_call
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id is None:
                i -= 1
                continue
            j = i - 1
            while j >= 0:
                prev = messages[j]
                if getattr(prev, "role", None) == "assistant" and getattr(prev, "tool_calls", None):
                    for tc in prev.tool_calls:
                        if (tc or {}).get("id") == tc_id:
                            tool_names.append(tool_name)
                            break
                    break
                j -= 1
        i -= 1

    if not tool_names:
        return None

    lines: List[str] = [
        "The parent agent already made these calls. This is metadata, not task guidance; "
        "use the task and your own inspection to decide whether another call is needed.",
        "",
    ]
    lines.extend(f"- **{tool_name}**" for tool_name in reversed(tool_names))

    return "\n".join(lines).rstrip()


def _get_role_instructions(role: Optional[str]) -> str:
    """Get role-specific instructions."""
    if not role:
        return ""
    return (
        f"You are acting as a specialist with the role: {role}. "
        "Apply your domain expertise to the task. Be thorough and precise."
    )


class DelegateTaskParams(BaseModel):
    task_description: str = Field(
        ...,
        description=(
            "A detailed description of the task for the sub-agent to accomplish. "
            "Be specific: include file paths, function names, and expected output format."
        ),
    )
    agent_role: Optional[str] = Field(
        None,
        description=(
            "Optional role/persona for the sub-agent. You can use a persona file "
            "name like 'code-reviewer' or a natural alias like 'Code Reviewer'. "
            "If no matching persona exists, the role falls back to generic "
            "role-specific guidance."
        ),
    )
    context_hints: Optional[List[str]] = Field(
        None,
        description=(
            "Optional list of file paths or short notes to give the sub-agent "
            "extra context about the project (e.g., ['src/auth.py', 'Uses JWT tokens'])."
        ),
    )
    model: Optional[str] = Field(
        None,
        description=(
            "Optional model override for this sub-agent (e.g., 'gpt-5.4-mini', "
            "'claude-3.5-haiku'). Defaults to the current model. DO NOT override "
            "the model unless explicitly requested by the user."
        ),
    )
    inherit_project_context: bool = Field(
        True,
        description=(
            "If true, the sub-agent inherits all currently pinned project context "
            "files and project instructions. Set to false for lightweight web research "
            "or tasks that don't need access to the local codebase to save tokens."
        ),
    )
    read_only_task: bool = Field(
        False,
        description=(
            "Set to True when the sub-agent will only read files, search code, or do research — "
            "it will NOT write files, run git commands, or modify any workspace state. "
            "Leave False (default) for any task that modifies files, runs git commands, or "
            "otherwise mutates workspace state."
        ),
    )
    isolation_domain: Literal["auto", "read_only", "browser", "desktop", "workspace"] = Field(
        "auto",
        description=(
            "Resource isolation domain for parallel mutating delegations. "
            "Use 'browser' for Playwright browser automation, 'desktop' for AppleScript/macOS UI, "
            "'workspace' for file/git edits (serial), 'read_only' equivalent to read_only_task=True. "
            "'auto' defaults to workspace (serial). Non-workspace domains can run in parallel "
            "(up to max_concurrent_mutating_subagents, default 3)."
        ),
    )
    task_id: Optional[str] = Field(
        None,
        description=(
            "This should only be set if you mean to resume a previous task (you can pass a "
            "prior task_id and the task will continue the same subagent session as before "
            "instead of creating a fresh one). The task_id is returned in the output of a "
            "previous delegate_task call."
        ),
    )


class DelegateTaskTool(Tool):
    """Tool for spawning isolated sub-agents to handle complex tasks."""

    name = "delegate_task"
    description = (
        "Delegate a complex, isolated, or multi-step task to a separate sub-agent. "
        "Useful for code review, security audit, research, refactoring analysis, "
        "or gathering specific information without filling up your own context window. "
        "The sub-agent has access to all the same tools, runs in an isolated session, "
        "and returns a comprehensive report. Provide a detailed task_description with "
        "specific file paths and expected output format for best results. "
        "Mutating delegations with isolation_domain='workspace' or 'auto' run one at a time. "
        "For parallel execution set isolation_domain='browser' (Playwright), 'desktop' "
        "(AppleScript), or read_only_task=True (research). Browser and desktop delegations "
        "each get isolated resources and can run concurrently (up to 3 by default). "
        "For pure research, set read_only_task=True — mutating tools are stripped and up to "
        "4 read-only delegations fan out in parallel."
    )
    category = "agent"
    # Routing is domain-aware in ``ToolExecutor.run_tool_batch`` — not via
    # ``max_parallel_invocations``. Kept at 0 so delegate_task is never
    # lumped into the generic capped-tools bucket.
    max_parallel_invocations = 0
    parameters_model = DelegateTaskParams
    is_read_only = False
    # The act of delegating is itself safe: the spawned sub-agent runs each of
    # its own tools through its own confirmation gate, so a mutating child tool
    # is still gated inside the child. Classified ``safe`` for now to preserve
    # the current no-confirmation delegation UX. NOTE: Phase 5.1 replaces this
    # with an explicit delegation gate + subset-privilege enforcement.
    safe = True

    # Fallback used when no parent config is available (e.g. during
    # auto-discovery before ``_configure_delegate_tool_context`` runs).
    # The live value resolves through ``self.timeout`` (a property).
    DEFAULT_TIMEOUT_SECONDS = 600.0

    def __init__(self) -> None:
        super().__init__()
        # ``context`` is populated by ``Agent._configure_delegate_tool_context``
        # once the parent agent is fully constructed.
        self.context: SubagentContext = SubagentContext()

    @property
    def timeout(self) -> float:  # type: ignore[override]
        """Per-invocation wall-clock cap, sourced from the parent agent's config.

        ``ToolExecutor.execute_single_tool`` reads this via ``getattr(tool, "timeout")``
        so a property is enough — no further plumbing required.
        """
        cfg = getattr(self.context, "parent_config", None)
        if cfg is not None:
            value = getattr(cfg, "subagent_timeout_seconds", None)
            if value:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return self.DEFAULT_TIMEOUT_SECONDS

    @property
    def _current_depth(self) -> int:
        """Owner-agent depth sourced from the structured context."""
        return self.context.delegation_depth

    @_current_depth.setter
    def _current_depth(self, value: int) -> None:
        self.context.delegation_depth = value

    async def execute(  # type: ignore[override]
        self,
        task_description: str,
        agent_role: Optional[str] = None,
        context_hints: Optional[List[str]] = None,
        model: Optional[str] = None,
        inherit_project_context: bool = True,
        read_only_task: bool = False,
        isolation_domain: str = "auto",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute the sub-agent delegation."""
        if self.context.delegation_depth >= MAX_DELEGATION_DEPTH:
            return {
                "success": False,
                "error": (
                    f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached. "
                    "Cannot spawn further sub-agents. Complete this task directly."
                ),
            }

        return await self._run_delegation(
            task_description,
            agent_role,
            context_hints,
            model,
            inherit_project_context,
            read_only_task,
            isolation_domain,
            task_id,
        )

    async def _run_delegation(
        self,
        task_description: str,
        agent_role: Optional[str],
        context_hints: Optional[List[str]],
        model: Optional[str],
        inherit_project_context: bool,
        read_only_task: bool = False,
        isolation_domain: str = "auto",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Core sub-agent spawning and execution logic."""
        sub_agent = None
        try:
            from coderAI.core.agent import Agent
            from coderAI.system.history import history_manager

            cwd = os.getcwd()
            ctx = self.context
            child_depth = ctx.delegation_depth + 1
            effective_domain = resolve_delegation_isolation_domain(
                {
                    "read_only_task": read_only_task,
                    "isolation_domain": isolation_domain,
                }
            )

            if child_depth > MAX_DELEGATION_DEPTH:
                return {
                    "success": False,
                    "error": (
                        f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached "
                        f"at construction time (would-be depth={child_depth})."
                    ),
                }

            # If task_id is provided, try to resume an existing sub-agent session
            resumed_session = None
            if task_id:
                try:
                    resumed_session = history_manager.load_session(task_id)
                    if resumed_session is not None:
                        parent_session_id = getattr(ctx.parent_session, "session_id", None)
                        if parent_session_id and resumed_session.session_id == parent_session_id:
                            return {
                                "success": False,
                                "error": (
                                    "Refusing to resume delegate_task with the parent session id. "
                                    "Pass the task_id returned by a previous delegate_task call."
                                ),
                                "error_code": ToolErrorCode.INVALID_TASK_ID,
                            }
                        logger.info(
                            "Resuming sub-agent session %s for task_id=%s",
                            task_id,
                            task_id,
                        )
                        event_emitter.emit(
                            "agent_status",
                            message=f"[dim]Resuming sub-agent session {task_id}...[/dim]",
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to resume sub-agent session %s: %s — starting fresh",
                        task_id,
                        e,
                    )

            logger.info(f"Sub-agent delegation: depth={child_depth}/{MAX_DELEGATION_DEPTH}")

            role_label = f" ({agent_role})" if agent_role else ""
            action = "Resuming" if resumed_session else "Spawning"
            event_emitter.emit(
                "agent_status",
                message=f"[bold purple]{action} Sub-Agent{role_label} (depth {child_depth}/{MAX_DELEGATION_DEPTH})...[/bold purple]",
            )

            # Inherit the parent agent's model when no explicit override is given
            effective_model = model or ctx.parent_model

            # Per-sub-agent cost is computed directly from this sub-agent's
            # own provider counters at the end of the run, not from a delta on
            # the shared cost tracker. The shared tracker drives the parent's
            # budget enforcement and is updated by both agents, so its delta
            # is unreliable when multiple read-only sub-agents run in parallel
            # (``MAX_CONCURRENT_READ_ONLY_SUBAGENTS=4``). See _final_cost_for.

            def _build_sub_agent():
                """Create and fully wire a fresh sub-agent."""
                nonlocal sub_agent
                sub_agent = Agent(
                    model=effective_model,
                    auto_approve=ctx.parent_auto_approve,
                    is_subagent=True,
                    delegation_depth=child_depth,
                )
                sub_agent.ipc_server = ctx.parent_ipc_server

                # Phase 5.2: inherit the parent's confirmation policy (e.g. the
                # headless deny-on-mutate guard) so a child's mutating tools face
                # the same gate and denials land in the parent's audit list.
                sub_agent.confirmation_override = ctx.parent_confirmation_override

                # Phase 5.4: one delegation-tree-wide cost budget. The cost
                # tracker is shared (assigned below), so pinning the child's cap
                # to the parent's makes check_budget_limit trip for the whole
                # tree — a child can never spend past the parent's ceiling.
                if ctx.parent_config is not None and sub_agent.config is not None:
                    sub_agent.config.budget_limit = ctx.parent_config.budget_limit

                persona = None
                if agent_role:
                    persona = sub_agent.set_persona(agent_role, update_model=False)

                if inherit_project_context and ctx.parent_context_controller is not None:
                    sub_agent.context_controller.copy_pinned_state_from(
                        ctx.parent_context_controller
                    )
                else:
                    sub_agent.context_controller.project_instructions = None
                    sub_agent.context_controller._instructions_loaded = True

                domain_capabilities = NATIVE_CAPABILITY_SETS[effective_domain]
                for name, child_tool in list(sub_agent.tools.tools.items()):
                    allowed = name in domain_capabilities
                    if effective_domain == "read_only":
                        allowed = allowed and bool(getattr(child_tool, "is_read_only", False))
                    if not allowed:
                        del sub_agent.tools.tools[name]

                # Phase 5.1: enforce child capability ⊆ parent. After persona
                # and read-only filtering, drop any tool the parent lacks so a
                # delegation — or a model-chosen agent_role / isolation_domain /
                # read_only_task=False — can never widen capability beyond the
                # parent (e.g. web tools stay gone when web_tools_in_main is off).
                if ctx.parent_tool_names is not None:
                    for name in list(sub_agent.tools.tools.keys()):
                        if name not in ctx.parent_tool_names:
                            del sub_agent.tools.tools[name]

                # The executor re-checks this exact set before dispatch, so a
                # hidden/invented call cannot bypass schema filtering. Dynamic
                # MCP is disabled for every domain-scoped sub-agent: MCP server
                # annotations are untrusted and there is currently no local,
                # exact server/tool read-only trust store.
                sub_agent._capability_domain = effective_domain
                sub_agent._allowed_native_tool_names = frozenset(sub_agent.tools.tools.keys())
                sub_agent._allow_dynamic_mcp = False

                # Configure the child's own delegation context AFTER all tool
                # filtering so any grandchild inherits the correct (narrowed)
                # capability ceiling and confirmation policy.
                sub_agent._configure_delegate_tool_context()

                if resumed_session is not None:
                    # Continue the same persisted sub-agent session. Keep the
                    # original session id and metadata so task_id remains
                    # stable across repeated resume calls.
                    sub_agent.session = resumed_session
                    if sub_agent.session is not None:
                        if effective_model:
                            sub_agent.session.model = effective_model
                        sub_agent.session.updated_at = _time.time()
                else:
                    # Sub-agents share project state with the parent, so their
                    # session bootstrap must not clear the parent's active plan.
                    sub_agent.create_session()
                    if sub_agent.session is not None:
                        sub_agent.session.metadata.update(
                            {
                                "purpose": "delegation",
                                "parent_session_id": getattr(
                                    ctx.parent_session, "session_id", None
                                ),
                                "delegation_depth": child_depth,
                                "agent_role": agent_role,
                                "isolation_domain": effective_domain,
                            }
                        )

                if ctx.parent_cost_tracker is not None:
                    # Assign after session bootstrap so create_session() never
                    # resets the parent's shared budget tracker.
                    sub_agent.cost_tracker = ctx.parent_cost_tracker
                    sub_agent.context_controller.cost_tracker = sub_agent.cost_tracker

                if ctx.parent_read_cache is not None:
                    sub_agent.read_cache = ctx.parent_read_cache
                    sub_agent._wire_read_cache()

                sub_agent._register_tracker(
                    task=task_description[:120],
                    role=agent_role,
                    parent_id=ctx.parent_agent_id,
                )

                return sub_agent, persona

            sub_agent, applied_persona = _build_sub_agent()

            # Build a rich system prompt overlay for the sub-agent (only for new sessions)
            if not resumed_session:
                role_instructions = "" if applied_persona else _get_role_instructions(agent_role)

                system_preamble_parts = [
                    "You are a sub-agent. Complete the assigned task autonomously.",
                    f"Project directory: {cwd}",
                    "Use tools to gather facts — do not guess. Cite file paths and line numbers.",
                    "Final turn must be a plain-text report (Summary, Findings, Recommendations).",
                    "Do not end on a tool call; an empty final turn is a failure.",
                ]

                if ctx.parent_cost_tracker is not None:
                    system_preamble_parts.append(
                        "- Your token spend counts against the parent's budget — be efficient."
                    )

                if role_instructions:
                    system_preamble_parts.extend(
                        ["", f"ROLE-SPECIFIC GUIDANCE ({agent_role}):", role_instructions]
                    )

                parent_history_note = _summarize_parent_tool_history(ctx.parent_session)
                if parent_history_note:
                    system_preamble_parts.extend(
                        [
                            "",
                            "PARENT AGENT TOOL HISTORY (recent):",
                            parent_history_note,
                        ]
                    )

                system_preamble = "\n".join(system_preamble_parts)

                found_system = False
                for msg in sub_agent.session.messages:
                    if msg.role == "system":
                        msg.content = f"{system_preamble}\n\n---\n\n{msg.content}"
                        found_system = True
                        break
                if not found_system:
                    sub_agent.session.add_message("system", system_preamble)

            # Add context hints to the task description directly
            if context_hints:
                hint_parts = ["CONTEXT PROVIDED BY PARENT AGENT:"]
                for hint in context_hints:
                    hint_parts.append(f"  - {hint}")
                task_description = (
                    "\n".join(hint_parts) + "\n\n---\n\nTASK DESCRIPTION:\n" + task_description
                )

            truncated_desc = task_description[:80] + ("..." if len(task_description) > 80 else "")
            event_emitter.emit(
                "agent_status",
                message=f"[dim]Sub-Agent working on: {truncated_desc}[/dim]",
            )

            # Process the task with progress reporting
            def _on_tool_progress(tool_calls, did_error):
                info = sub_agent.tracker_info
                if not info:
                    return
                if tool_calls:
                    names = [
                        tc.get("function", {}).get("name", tc.get("name", "?"))
                        for tc in tool_calls[:3]
                    ]
                    info.current_tool = ", ".join(names)
                    if len(tool_calls) > 3:
                        info.current_tool += f" +{len(tool_calls) - 3} more"
                else:
                    info.current_tool = None
                info.status = AgentStatus.TOOL_CALL if not did_error else AgentStatus.THINKING
                event_emitter.emit("agent_tracker_sync", info=info)

            try:
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        final_report = await sub_agent.process_single_shot(
                            task_description, progress_callback=_on_tool_progress
                        )
                        break
                    except Exception as retry_exc:
                        cancel_evt = (
                            sub_agent.tracker_info._cancel_event if sub_agent.tracker_info else None
                        )
                        if (
                            attempt > MAX_DELEGATION_RETRIES
                            or isinstance(retry_exc, BudgetExceededError)
                            or not is_transient_error(retry_exc)
                            or (cancel_evt is not None and cancel_evt.is_set())
                        ):
                            raise
                        delay = backoff_delay(
                            attempt, base=1.0, cap=DELEGATION_RETRY_DELAY_CAP_SECONDS
                        )
                        logger.warning(
                            "Sub-agent transient failure (attempt %d/%d) — retrying in %.1fs: %s",
                            attempt,
                            MAX_DELEGATION_RETRIES + 1,
                            delay,
                            retry_exc,
                        )
                        event_emitter.emit(
                            "agent_status",
                            message=(
                                f"[dim]Sub-Agent{role_label} hit a transient error — "
                                f"retrying (attempt {attempt + 1}/{MAX_DELEGATION_RETRIES + 1})…[/dim]"
                            ),
                        )
                        await asyncio.sleep(delay)

                if not (final_report and final_report.strip()):
                    from coderAI.core.agent_loop import ExecutionLoop

                    logger.warning("Sub-agent returned empty report — trying closing summary.")
                    event_emitter.emit(
                        "agent_status",
                        message=f"[dim]Sub-Agent{role_label} report was empty — requesting summary…[/dim]",
                    )
                    try:
                        closing = await ExecutionLoop(sub_agent)._post_tool_closing_message(
                            task_description
                        )
                        if closing and closing.strip():
                            final_report = closing.strip()
                            sub_agent.session.add_message("assistant", final_report)
                            sub_agent.save_session()
                    except Exception as closing_err:
                        logger.warning("Sub-agent closing summary failed: %s", closing_err)

                if not (final_report and final_report.strip()):
                    logger.warning("Sub-agent still empty — requesting full-context retry.")
                    retry_prompt = (
                        "Your previous response was empty. You have been working on the "
                        "following task and have used tools to complete it. Please now write "
                        "a comprehensive final report summarizing:\n"
                        "1. What actions you took (files created/modified, commands run)\n"
                        "2. The outcome of each action\n"
                        "3. Any issues encountered\n"
                        "4. Current status and next steps\n\n"
                        f"Task (with context hints): {task_description[:2000]}"
                    )
                    sub_agent.session.add_message("user", retry_prompt)
                    retry_messages = sub_agent.session.get_messages_for_api()
                    # Call LLM without tools to force a text-only response.
                    # ``tools=None`` is also what keeps this retry inside the
                    # delegation depth budget: with no tools advertised, the
                    # model cannot invoke ``delegate_task`` again and so cannot
                    # spawn an off-the-books grand-child past
                    # ``MAX_DELEGATION_DEPTH``.
                    try:
                        # Snapshot provider counters before the retry call so
                        # we can attribute incremental tokens to this retry.
                        mi_before = sub_agent.provider.get_model_info()
                        retry_resp = await sub_agent.provider.chat(retry_messages, tools=None)
                        mi_after = sub_agent.provider.get_model_info()
                        new_in = mi_after.get("total_input_tokens", 0) - mi_before.get(
                            "total_input_tokens", 0
                        )
                        new_out = mi_after.get("total_output_tokens", 0) - mi_before.get(
                            "total_output_tokens", 0
                        )
                        sub_agent.total_prompt_tokens = mi_after.get("total_input_tokens", 0)
                        sub_agent.total_completion_tokens = mi_after.get("total_output_tokens", 0)
                        sub_agent.total_tokens = mi_after.get("total_tokens", 0)
                        if (new_in > 0 or new_out > 0) and sub_agent.cost_tracker is not None:
                            model_for_cost = getattr(
                                sub_agent.provider, "actual_model", sub_agent.model
                            )
                            await sub_agent.cost_tracker.add_cost(model_for_cost, new_in, new_out)
                        choices = retry_resp.get("choices", [])
                        if choices:
                            final_report = choices[0].get("message", {}).get("content") or ""
                            # Persist the retry reply so a later task_id resume
                            # doesn't see a dangling user prompt with no answer.
                            if final_report:
                                sub_agent.session.add_message("assistant", final_report)
                                sub_agent.save_session()
                    except Exception as retry_err:
                        logger.warning(f"Sub-agent summary retry failed: {retry_err}")

                # Fallback: synthesize report from tool results in session
                if not (final_report and final_report.strip()):
                    logger.warning(
                        "Sub-agent still empty after retry — synthesizing from tool results."
                    )
                    final_report = self._synthesize_fallback_report(
                        sub_agent, task_description, agent_role
                    )

                if not (final_report and final_report.strip()):
                    raise RuntimeError("Sub-agent produced no final report text.")

            except Exception as e:
                logger.error(f"Sub-agent failed: {e}")
                wasted_tokens = getattr(sub_agent, "total_tokens", 0) if sub_agent else 0
                from coderAI.system.cost import CostTracker

                wasted_cost = self._final_cost_for(sub_agent)
                event_emitter.emit(
                    "agent_error",
                    message=(
                        f"Sub-Agent{role_label} failed: {e}. Tokens burned: {wasted_tokens:,} | "
                        f"Cost: {CostTracker.format_cost(wasted_cost)}"
                    ),
                )
                return {
                    "success": False,
                    "error": f"Sub-agent failed: {e}",
                    "sub_agent_role": agent_role or "General Assistant",
                    "tokens_used": wasted_tokens,
                    "cost_usd": wasted_cost,
                }

            tokens_used = sub_agent.total_tokens
            cost_usd = self._final_cost_for(sub_agent)

            # Run on_subagent_stop hooks on the parent agent. The whole chain
            # (parent_ipc_server → agent → hooks_manager) is optional in
            # headless / test setups, so guard each hop instead of letting an
            # AttributeError get caught as a generic delegation failure.
            parent_hooks_manager = None
            parent_ipc = self.context.parent_ipc_server
            parent_for_hooks = getattr(parent_ipc, "agent", None) if parent_ipc else None
            if parent_for_hooks is not None:
                parent_hooks_manager = getattr(parent_for_hooks, "hooks_manager", None)

            hooks_data = parent_hooks_manager.load_hooks() if parent_hooks_manager else None
            if parent_hooks_manager is not None and hooks_data:
                await parent_hooks_manager.run_hooks(
                    "delegate_task",
                    "on_subagent_stop",
                    {"task": task_description, "report": final_report, "tokens": tokens_used},
                    hooks_data,
                )

            from coderAI.system.cost import CostTracker

            event_emitter.emit(
                "agent_status",
                message=(
                    f"[bold green]Sub-Agent{role_label} finished.[/bold green] "
                    f"[dim]Tokens: {tokens_used:,} | "
                    f"Cost: {CostTracker.format_cost(cost_usd)}[/dim]"
                ),
            )

            task_session_id = getattr(sub_agent.session, "session_id", None)
            return {
                "success": True,
                "sub_agent_role": agent_role or "General Assistant",
                "sub_agent_model": sub_agent.model,
                "final_report": final_report,
                "tokens_used": tokens_used,
                "cost_usd": cost_usd,
                **(
                    {
                        "task_id": task_session_id,
                        "note": "Pass this task_id to future delegate_task calls with the same subagent_type to resume this session.",
                    }
                    if task_session_id
                    else {}
                ),
            }

        except Exception as e:
            logger.error(f"Error during sub-agent delegation: {e}", exc_info=True)
            event_emitter.emit("agent_error", message=f"Sub-Agent failed: {str(e)}")
            return {"success": False, "error": str(e)}
        finally:
            # Always close the sub-agent to release HTTP sessions and other resources
            if sub_agent is not None:
                agent_id = getattr(getattr(sub_agent, "tracker_info", None), "agent_id", None)
                if agent_id:
                    try:
                        from coderAI.tools.browser import BrowserRegistry

                        await BrowserRegistry.get().close_agent(agent_id)
                    except Exception:
                        pass
                try:
                    await sub_agent.close()
                except Exception:
                    pass

    @staticmethod
    def _final_cost_for(sub_agent: Any) -> float:
        """Compute cost from the sub-agent's own provider token counters.

        Reading the shared cost tracker delta is unreliable when multiple
        sub-agents run in parallel — concurrent spend is cross-attributed.
        Computing from this sub-agent's own ``total_prompt_tokens`` and
        ``total_completion_tokens`` against its provider's model price gives
        a clean per-agent number.
        """
        if sub_agent is None:
            return 0.0
        from coderAI.system.cost import CostTracker

        provider = getattr(sub_agent, "provider", None)
        model_val = getattr(provider, "actual_model", None) or getattr(sub_agent, "model", "")
        model_for_cost = str(model_val) if model_val is not None else ""
        try:
            return CostTracker.calculate_cost_for_tokens(
                model_for_cost,
                int(getattr(sub_agent, "total_prompt_tokens", 0) or 0),
                int(getattr(sub_agent, "total_completion_tokens", 0) or 0),
            )
        except Exception:
            return 0.0

    @staticmethod
    def _synthesize_fallback_report(
        sub_agent: Any, task_description: str, agent_role: Optional[str]
    ) -> str:
        """Build a best-effort report from the sub-agent's session when the
        LLM failed to produce one.

        Walks the session messages looking for tool calls and their results,
        and assembles a structured summary so the parent agent (and user)
        can see what actually happened.
        """
        import json as _json

        if sub_agent.session is None:
            return ""

        tool_summaries: List[str] = []
        assistant_texts: List[str] = []

        for msg in sub_agent.session.messages:
            if msg.role == "assistant" and isinstance(msg.content, str) and msg.content.strip():
                assistant_texts.append(msg.content.strip())

            if msg.role == "tool" and msg.content:
                tool_name = msg.name or "unknown_tool"
                if isinstance(msg.content, str):
                    try:
                        parsed = _json.loads(msg.content)
                        success = parsed.get("success", "?")
                        detail = str(parsed.get("output", parsed.get("error", "")))[:300]
                    except (_json.JSONDecodeError, AttributeError):
                        success = "?"
                        detail = str(msg.content)[:300]
                else:
                    success = "?"
                    detail = str(msg.content)[:300]
                tool_summaries.append(
                    f"- **{tool_name}**: success={success}" + (f" — {detail}" if detail else "")
                )

        if not tool_summaries and not assistant_texts:
            return ""

        parts = [
            "## Fallback Report (auto-generated)",
            f"**Role:** {agent_role or 'General Assistant'}",
            f"**Task:** {task_description[:500]}",
            "",
        ]

        if assistant_texts:
            parts.append("### Assistant Notes")
            # Include last few assistant messages — most likely to be relevant
            for text in assistant_texts[-3:]:
                parts.append(text[:500])
            parts.append("")

        if tool_summaries:
            parts.append(f"### Tool Activity ({len(tool_summaries)} calls)")
            parts.extend(tool_summaries[-30:])  # Cap to avoid giant reports
            if len(tool_summaries) > 30:
                parts.append(f"_(… and {len(tool_summaries) - 30} earlier tool calls omitted)_")

        return "\n".join(parts)
