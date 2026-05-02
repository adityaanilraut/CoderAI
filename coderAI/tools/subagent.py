"""Sub-agent delegation tool for multi-agent capabilities."""

import logging
import os
import time as _time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..agent_tracker import AgentStatus
from ..events import event_emitter

logger = logging.getLogger(__name__)

# Maximum depth for nested sub-agent delegation to prevent infinite recursion
MAX_DELEGATION_DEPTH = 3


@dataclass
class SubagentContext:
    """References from the parent Agent needed by DelegateTaskTool."""

    parent_agent_id: Optional[str] = None
    parent_model: Optional[str] = None
    parent_context_manager: Optional[Any] = None  # ContextManager
    parent_cost_tracker: Optional[Any] = None  # CostTracker (shared)
    parent_auto_approve: bool = False
    parent_ipc_server: Optional[Any] = None
    parent_session: Optional[Any] = None  # Session
    delegation_depth: int = 0

# Number of recent parent tool calls to summarise for the sub-agent so it
# doesn't repeat inspection work the parent has already done.
RECENT_TOOL_HISTORY_LIMIT = 10


def _summarize_parent_tool_history(
    parent_session: Any, limit: int = RECENT_TOOL_HISTORY_LIMIT
) -> Optional[str]:
    """Build a short markdown summary of the parent's most recent tool calls.

    Returns ``None`` when there is nothing useful to share. Each entry shows
    the tool name, the arguments, and a very compact preview of the result so
    the sub-agent can see what has already been inspected.
    """
    import json as _json

    if parent_session is None:
        return None
    messages = getattr(parent_session, "messages", None) or []
    if not messages:
        return None

    # Walk backwards collecting (assistant tool_call, tool result) pairs.
    pairs: List[tuple] = []
    i = len(messages) - 1
    while i >= 0 and len(pairs) < limit:
        msg = messages[i]
        if getattr(msg, "role", None) == "tool":
            tool_name = getattr(msg, "name", None) or "unknown"
            result = (getattr(msg, "content", None) or "")[:240]
            # Find the preceding assistant message that emitted this tool_call
            tc_id = getattr(msg, "tool_call_id", None)
            args_preview = ""
            if tc_id is None:
                i -= 1
                continue
            j = i - 1
            while j >= 0:
                prev = messages[j]
                if getattr(prev, "role", None) == "assistant" and getattr(prev, "tool_calls", None):
                    for tc in prev.tool_calls:
                        if (tc or {}).get("id") == tc_id:
                            raw = (tc.get("function") or {}).get("arguments")
                            if isinstance(raw, str):
                                args_preview = raw[:160]
                            else:
                                try:
                                    args_preview = _json.dumps(raw)[:160]
                                except Exception:
                                    args_preview = str(raw)[:160]
                            break
                    break
                j -= 1
            pairs.append((tool_name, args_preview, result))
        i -= 1

    if not pairs:
        return None

    lines = [
        "The parent agent has already made these tool calls during the current session.",
        "Use them as prior knowledge — do NOT repeat these exact calls unless the state has demonstrably changed.",
        "",
    ]
    for tool_name, args_preview, result in reversed(pairs):
        line = f"- **{tool_name}**"
        if args_preview:
            line += f" `{args_preview}`"
        if result:
            preview = result.replace("\n", " ")
            line += f" → {preview}"
        lines.append(line)
    return "\n".join(lines)

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
        "Mutating delegations run one at a time to prevent workspace conflicts. "
        "For pure research or read-only work, set read_only_task=True — such "
        "delegations have mutating tools stripped and are fanned out in parallel "
        "(up to 4 at a time), dramatically reducing wall time when you spawn "
        "several specialists that don't touch the filesystem."
    )
    # Default to serialising sub-agents: mutating delegations share the
    # workspace (git branch, file tree) and must not race. Read-only
    # delegations (``read_only_task=True``) bypass this cap — see
    # ``ToolExecutor.run_tool_batch`` which routes them to a bounded
    # parallel group.
    max_parallel_invocations = 1
    parameters_model = DelegateTaskParams
    is_read_only = False
    timeout = 600.0  # 10 minutes; not yet configurable via standard Tool settings

    def __init__(self) -> None:
        super().__init__()
        # ``context`` is populated by ``Agent._configure_delegate_tool_context``
        # once the parent agent is fully constructed.
        self.context: SubagentContext = SubagentContext()

    @property
    def _current_depth(self) -> int:
        """Owner-agent depth sourced from the structured context."""
        return self.context.delegation_depth

    @_current_depth.setter
    def _current_depth(self, value: int) -> None:
        self.context.delegation_depth = value

    async def execute(
        self,
        task_description: str,
        agent_role: Optional[str] = None,
        context_hints: Optional[List[str]] = None,
        model: Optional[str] = None,
        inherit_project_context: bool = True,
        read_only_task: bool = False,
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
            task_description, agent_role, context_hints, model, inherit_project_context, read_only_task, task_id
        )

    async def _run_delegation(
        self,
        task_description: str,
        agent_role: Optional[str],
        context_hints: Optional[List[str]],
        model: Optional[str],
        inherit_project_context: bool,
        read_only_task: bool = False,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Core sub-agent spawning and execution logic."""
        sub_agent = None
        try:
            from ..agent import Agent
            from ..history import history_manager

            cwd = os.getcwd()
            ctx = self.context
            child_depth = ctx.delegation_depth + 1

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
                        logger.info(
                            "Resuming sub-agent session %s for task_id=%s",
                            task_id, task_id,
                        )
                        event_emitter.emit(
                            "agent_status",
                            message=f"[dim]Resuming sub-agent session {task_id}...[/dim]",
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to resume sub-agent session %s: %s — starting fresh",
                        task_id, e,
                    )

            logger.info(
                f"Sub-agent delegation: depth={child_depth}/{MAX_DELEGATION_DEPTH}"
            )

            role_label = f" ({agent_role})" if agent_role else ""
            action = "Resuming" if resumed_session else "Spawning"
            event_emitter.emit(
                "agent_status",
                message=f"[bold purple]{action} Sub-Agent{role_label} (depth {child_depth}/{MAX_DELEGATION_DEPTH})...[/bold purple]",
            )

            # Inherit the parent agent's model when no explicit override is given
            effective_model = model or ctx.parent_model

            # Snapshot cost BEFORE spawning so we can attribute spend to this
            # sub-agent. NOTE: the cost tracker is shared across all agents, so
            # the delta ``final - snapshot`` may include cost from other
            # concurrently running read-only sub-agents — the per-agent cost
            # is therefore only accurate when no other sub-agents are active.
            parent_cost_before = (
                ctx.parent_cost_tracker.get_total_cost()
                if ctx.parent_cost_tracker is not None
                else 0.0
            )

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

                if ctx.parent_cost_tracker is not None:
                    sub_agent.cost_tracker = ctx.parent_cost_tracker

                persona = None
                if agent_role:
                    persona = sub_agent.set_persona(
                        agent_role,
                        update_model=model is None,
                    )

                sub_agent._configure_delegate_tool_context()

                if (
                    inherit_project_context
                    and ctx.parent_context_manager is not None
                ):
                    sub_agent.context_manager.pinned_files = dict(
                        ctx.parent_context_manager.pinned_files
                    )
                    sub_agent.context_manager._pinned_mtimes = dict(
                        ctx.parent_context_manager._pinned_mtimes
                    )
                    if ctx.parent_context_manager.project_instructions:
                        sub_agent.context_manager.project_instructions = (
                            ctx.parent_context_manager.project_instructions
                        )
                        sub_agent.context_manager._instructions_loaded = True
                else:
                    sub_agent.context_manager.project_instructions = None
                    sub_agent.context_manager._instructions_loaded = True

                if read_only_task:
                    mutating = [
                        name
                        for name, t in sub_agent.tools.tools.items()
                        if not getattr(t, "is_read_only", False)
                    ]
                    for name in mutating:
                        del sub_agent.tools.tools[name]

                sub_agent.create_session()
                if resumed_session is not None:
                    # Clone messages from the resumed session so the sub-agent
                    # continues where it left off.
                    sub_agent.session.messages = list(resumed_session.messages)
                    sub_agent.session.model = effective_model
                    sub_agent.session.updated_at = _time.time()

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
                    "You are a specialized sub-agent spawned by a parent CoderAI agent.",
                    f"You are working in the project directory: {cwd}",
                    "",
                    "IMPORTANT INSTRUCTIONS:",
                    "- Complete the assigned task thoroughly and autonomously.",
                    "- Use tools (read_file, grep, run_command, etc.) to gather information — do NOT guess.",
                    "- Provide a comprehensive, well-structured final report.",
                    "- Structure your report with clear sections: Summary, Findings, Recommendations.",
                    "- Be specific: cite file paths, line numbers, and code snippets.",
                    "- Do NOT ask questions — make reasonable assumptions and note them.",
                    "- WEB SEARCH: If `web_search` or `read_url` appear under **Available Tools** in your system prompt, "
                    "call them directly to retrieve web content — NEVER tell the parent agent or user to run curl/wget "
                    "themselves. Include the fetched content directly in your report.",
                    "- Do NOT parse HTML or scrape web pages with shell pipelines (`curl | grep | sed`). "
                    "Use `web_search`/`read_url` if available, otherwise a short Python script.",
                    "- Do NOT switch branches unless explicitly required by the task.",
                    "- CRITICAL: Your FINAL turn MUST be a plain-text assistant message "
                    "containing the full report. Do NOT end the conversation on a "
                    "tool call — after you have gathered enough information, stop "
                    "calling tools and write the report as text. An empty or "
                    "tool-call-only final turn is considered a failure.",
                ]

                if role_instructions:
                    system_preamble_parts.extend(["", f"ROLE-SPECIFIC GUIDANCE ({agent_role}):", role_instructions])

                parent_history_note = _summarize_parent_tool_history(ctx.parent_session)
                if parent_history_note:
                    system_preamble_parts.extend([
                        "",
                        "PARENT AGENT TOOL HISTORY (recent):",
                        parent_history_note,
                    ])

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
                task_description = "\n".join(hint_parts) + "\n\n---\n\nTASK DESCRIPTION:\n" + task_description

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
                    names = [tc.get("function", {}).get("name", tc.get("name", "?"))
                            for tc in tool_calls[:3]]
                    info.current_tool = ", ".join(names)
                    if len(tool_calls) > 3:
                        info.current_tool += f" +{len(tool_calls) - 3} more"
                else:
                    info.current_tool = None
                info.status = AgentStatus.TOOL_CALL if not did_error else AgentStatus.THINKING
                event_emitter.emit("agent_tracker_sync", info=info)

            try:
                final_report = await sub_agent.process_single_shot(
                    task_description, progress_callback=_on_tool_progress
                )

                # Retry: if the report is empty, ask once more for a summary
                if not (final_report and final_report.strip()):
                    logger.warning(
                        "Sub-agent returned empty report — requesting summary retry."
                    )
                    event_emitter.emit(
                        "agent_status",
                        message=f"[dim]Sub-Agent{role_label} report was empty — retrying summary…[/dim]",
                    )

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
                        retry_resp = await sub_agent.provider.chat(
                            retry_messages, tools=None
                        )
                        mi_after = sub_agent.provider.get_model_info()
                        new_in = mi_after.get("total_input_tokens", 0) - mi_before.get(
                            "total_input_tokens", 0
                        )
                        new_out = mi_after.get(
                            "total_output_tokens", 0
                        ) - mi_before.get("total_output_tokens", 0)
                        sub_agent.total_prompt_tokens = mi_after.get(
                            "total_input_tokens", 0
                        )
                        sub_agent.total_completion_tokens = mi_after.get(
                            "total_output_tokens", 0
                        )
                        sub_agent.total_tokens = mi_after.get("total_tokens", 0)
                        if (new_in > 0 or new_out > 0) and sub_agent.cost_tracker is not None:
                            model_for_cost = getattr(
                                sub_agent.provider, "actual_model", sub_agent.model
                            )
                            sub_agent.cost_tracker.add_cost(
                                model_for_cost, new_in, new_out
                            )
                        choices = retry_resp.get("choices", [])
                        if choices:
                            final_report = (
                                choices[0].get("message", {}).get("content") or ""
                            )
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
                wasted_cost = 0.0
                if sub_agent is not None and getattr(sub_agent, "cost_tracker", None):
                    try:
                        wasted_cost = sub_agent.cost_tracker.get_total_cost() - parent_cost_before
                    except Exception:
                        pass
                from ..cost import CostTracker
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
            cost_usd = sub_agent.cost_tracker.get_total_cost() - parent_cost_before

            sub_agent._finish_tracker()

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
            if hooks_data:
                await parent_hooks_manager.run_hooks(
                    "delegate_task", "on_subagent_stop",
                    {"task": task_description, "report": final_report, "tokens": tokens_used},
                    hooks_data,
                )

            from ..cost import CostTracker
            event_emitter.emit(
                "agent_status",
                message=(
                    f"[bold green]Sub-Agent{role_label} finished.[/bold green] "
                    f"[dim]Tokens: {tokens_used:,} | "
                    f"Cost: {CostTracker.format_cost(cost_usd)}[/dim]"
                ),
            )

            task_session_id = getattr(sub_agent.session, "id", None)
            return {
                "success": True,
                "sub_agent_role": agent_role or "General Assistant",
                "sub_agent_model": sub_agent.model,
                "final_report": final_report,
                "tokens_used": tokens_used,
                "cost_usd": cost_usd,
                **(
                    {"task_id": task_session_id, "note": "Pass this task_id to future delegate_task calls with the same subagent_type to resume this session."}
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
                try:
                    await sub_agent.close()
                except Exception:
                    pass

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
            if (
                msg.role == "assistant"
                and isinstance(msg.content, str)
                and msg.content.strip()
            ):
                assistant_texts.append(msg.content.strip())

            if msg.role == "tool" and msg.content:
                tool_name = msg.name or "unknown_tool"
                if isinstance(msg.content, str):
                    try:
                        parsed = _json.loads(msg.content)
                        success = parsed.get("success", "?")
                        detail = str(
                            parsed.get("output", parsed.get("error", ""))
                        )[:300]
                    except (_json.JSONDecodeError, AttributeError):
                        success = "?"
                        detail = str(msg.content)[:300]
                else:
                    success = "?"
                    detail = str(msg.content)[:300]
                tool_summaries.append(
                    f"- **{tool_name}**: success={success}"
                    + (f" — {detail}" if detail else "")
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
                parts.append(
                    f"_(… and {len(tool_summaries) - 30} earlier tool calls omitted)_"
                )

        return "\n".join(parts)
