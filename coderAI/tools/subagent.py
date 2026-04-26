"""Sub-agent delegation tool for multi-agent capabilities."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..events import event_emitter

logger = logging.getLogger(__name__)

# Maximum depth for nested sub-agent delegation to prevent infinite recursion
MAX_DELEGATION_DEPTH = 3


@dataclass
class SubagentContext:
    """Everything a ``DelegateTaskTool`` needs from its parent ``Agent``.

    Passed as a single attribute on the tool so the wiring is legible and
    tests can construct a realistic context without monkey-patching a
    handful of private attributes.
    """

    parent_agent_id: Optional[str] = None
    parent_model: Optional[str] = None
    parent_context_manager: Optional[Any] = None  # ContextManager
    parent_cost_tracker: Optional[Any] = None  # CostTracker (shared)
    parent_auto_approve: bool = False
    parent_ipc_server: Optional[Any] = None
    parent_session: Optional[Any] = None  # Session — used to surface recent tool findings

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
    timeout = 600.0

    def __init__(self) -> None:
        super().__init__()
        # ``context`` is populated by ``Agent._configure_delegate_tool_context``
        # once the parent agent is fully constructed.
        self.context: SubagentContext = SubagentContext()
        # Depth (root agent = 0) — incremented on each delegation hop.
        # Instance-level to avoid confusing class-attribute shadowing.
        self._current_depth: int = 0

    async def execute(
        self,
        task_description: str,
        agent_role: Optional[str] = None,
        context_hints: Optional[List[str]] = None,
        model: Optional[str] = None,
        inherit_project_context: bool = True,
        read_only_task: bool = False,
    ) -> Dict[str, Any]:
        """Execute the sub-agent delegation."""
        if self._current_depth >= MAX_DELEGATION_DEPTH:
            return {
                "success": False,
                "error": (
                    f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached. "
                    "Cannot spawn further sub-agents. Complete this task directly."
                ),
            }

        return await self._run_delegation(
            task_description, agent_role, context_hints, model, inherit_project_context, read_only_task
        )

    async def _run_delegation(
        self,
        task_description: str,
        agent_role: Optional[str],
        context_hints: Optional[List[str]],
        model: Optional[str],
        inherit_project_context: bool,
        read_only_task: bool = False,
    ) -> Dict[str, Any]:
        """Core sub-agent spawning and execution logic."""
        sub_agent = None
        try:
            from ..agent import Agent

            cwd = os.getcwd()
            logger.info(
                f"Sub-agent delegation: "
                f"depth={self._current_depth + 1}/{MAX_DELEGATION_DEPTH}"
            )

            role_label = f" ({agent_role})" if agent_role else ""
            event_emitter.emit(
                "agent_status",
                message=f"[bold purple]Spawning Sub-Agent{role_label} (depth {self._current_depth + 1}/{MAX_DELEGATION_DEPTH})...[/bold purple]",
            )

            ctx = self.context

            # Inherit the parent agent's model when no explicit override is given
            effective_model = model or ctx.parent_model

            # Snapshot the parent's cost so we can report only the sub-agent's
            # incremental spend later (the cost tracker is shared, so a raw read
            # of get_total_cost() would include everything the parent spent).
            parent_cost_before = (
                ctx.parent_cost_tracker.get_total_cost()
                if ctx.parent_cost_tracker is not None
                else 0.0
            )

            def _build_sub_agent():
                """Create and fully wire a fresh sub-agent.

                Factored out so the spawn path and the retry path share one
                implementation. Returns ``(agent, persona)``.
                """
                new_agent = Agent(
                    model=effective_model,
                    auto_approve=ctx.parent_auto_approve,
                    is_subagent=True,
                )
                new_agent.ipc_server = ctx.parent_ipc_server

                # Share parent's cost tracker BEFORE reconfiguring delegate
                # tool context — otherwise nested delegations (grand-children)
                # would account against an orphan tracker.
                if ctx.parent_cost_tracker is not None:
                    new_agent.cost_tracker = ctx.parent_cost_tracker

                persona = None
                if agent_role:
                    persona = new_agent.set_persona(
                        agent_role,
                        update_model=model is None,
                    )

                # Re-wire the child's delegate_task tool so its
                # ``context`` reflects the post-override state. ``set_persona``
                # already does this when a persona is applied, but not
                # otherwise.
                new_agent._configure_delegate_tool_context()

                # Inherit parent's pinned context.
                if (
                    inherit_project_context
                    and ctx.parent_context_manager is not None
                ):
                    new_agent.context_manager.pinned_files = dict(
                        ctx.parent_context_manager.pinned_files
                    )
                    new_agent.context_manager._pinned_mtimes = dict(
                        ctx.parent_context_manager._pinned_mtimes
                    )
                    if ctx.parent_context_manager.project_instructions:
                        new_agent.context_manager.project_instructions = (
                            ctx.parent_context_manager.project_instructions
                        )
                        new_agent.context_manager._instructions_loaded = True
                else:
                    # Explicitly mark as loaded with no content to prevent
                    # the sub-agent from lazily loading them from disk.
                    new_agent.context_manager.project_instructions = None
                    new_agent.context_manager._instructions_loaded = True

                # Strip mutating tools when the caller declared a read-only task
                if read_only_task:
                    mutating = [
                        name for name, t in new_agent.tools.tools.items()
                        if not getattr(t, "is_read_only", False)
                    ]
                    for name in mutating:
                        del new_agent.tools.tools[name]

                new_agent.create_session()

                new_agent._register_tracker(
                    task=task_description[:120],
                    role=agent_role,
                    parent_id=ctx.parent_agent_id,
                )

                # Propagate delegation depth so the sub-agent's
                # DelegateTaskTool enforces MAX_DELEGATION_DEPTH.
                child_delegate = new_agent.tools.get("delegate_task")
                if child_delegate is not None:
                    child_delegate._current_depth = self._current_depth + 1

                return new_agent, persona

            sub_agent, applied_persona = _build_sub_agent()

            # Build a rich system prompt overlay for the sub-agent
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

            # Prepend to the sub-agent's existing system prompt
            for msg in sub_agent.session.messages:
                if msg.role == "system":
                    msg.content = f"{system_preamble}\n\n---\n\n{msg.content}"
                    break

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

            # Process the task
            try:
                final_report = await sub_agent.process_single_shot(task_description)

                # ── Retry: if the report is empty, ask once more for a summary ──
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
                        f"Original task: {task_description[:2000]}"
                    )
                    sub_agent.session.add_message("user", retry_prompt)
                    retry_messages = sub_agent.session.get_messages_for_api()
                    # Call LLM without tools to force a text-only response
                    try:
                        retry_resp = await sub_agent.provider.chat(
                            retry_messages, tools=None
                        )
                        choices = retry_resp.get("choices", [])
                        if choices:
                            final_report = (
                                choices[0].get("message", {}).get("content") or ""
                            )
                    except Exception as retry_err:
                        logger.warning(f"Sub-agent summary retry failed: {retry_err}")

                # ── Fallback: synthesize report from tool results in session ──
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

            # Run on_subagent_stop hooks on the parent agent
            hooks_data = self.context.parent_ipc_server.agent.hooks_manager.load_hooks() if (self.context.parent_ipc_server and hasattr(self.context.parent_ipc_server, "agent")) else None
            if hooks_data:
                await self.context.parent_ipc_server.agent.hooks_manager.run_hooks(
                    "delegate_task", "on_subagent_stop", 
                    {"task": task_description, "report": final_report, "tokens": tokens_used}, 
                    hooks_data
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

            return {
                "success": True,
                "sub_agent_role": agent_role or "General Assistant",
                "sub_agent_model": sub_agent.model,
                "final_report": final_report,
                "tokens_used": tokens_used,
                "cost_usd": cost_usd,
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
            if msg.role == "assistant" and msg.content and msg.content.strip():
                assistant_texts.append(msg.content.strip())

            if msg.role == "tool" and msg.content:
                tool_name = msg.name or "unknown_tool"
                try:
                    parsed = _json.loads(msg.content)
                    success = parsed.get("success", "?")
                    # Keep tool result short for the fallback summary
                    detail = str(parsed.get("output", parsed.get("error", "")))[:300]
                except (_json.JSONDecodeError, AttributeError):
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
