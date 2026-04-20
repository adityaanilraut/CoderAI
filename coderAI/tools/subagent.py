"""Sub-agent delegation tool for multi-agent capabilities."""

import logging
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..events import event_emitter

logger = logging.getLogger(__name__)

# Maximum depth for nested sub-agent delegation to prevent infinite recursion
MAX_DELEGATION_DEPTH = 3

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
            "Optional model override for this sub-agent (e.g., 'gpt-5-mini', "
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
        "Sub-agents run sequentially to prevent conflicts during branch switching "
        "or file modifications."
    )
    # Sequential execution to avoid workspace state conflicts (branch switching, etc.)
    max_parallel_invocations = 1
    parameters_model = DelegateTaskParams
    # Sub-agents may run mutating tools; parallel runs can race on the same files
    # or git state.
    is_read_only = False

    # Set by the parent Agent after registration so the sub-agent can
    # inherit the model and link to the correct parent in the tracker.
    _parent_model: Optional[str] = None
    _parent_agent_id: Optional[str] = None
    _parent_context_manager: Optional[Any] = None  # parent's ContextManager
    _parent_cost_tracker: Optional[Any] = None  # parent's CostTracker (shared)
    _parent_auto_approve: bool = False  # Default to False for sub-agents for safety
    _current_depth: int = 0  # incremented on each delegation

    async def execute(
        self,
        task_description: str,
        agent_role: Optional[str] = None,
        context_hints: Optional[List[str]] = None,
        model: Optional[str] = None,
        inherit_project_context: bool = True,
    ) -> Dict[str, Any]:
        """Execute the sub-agent delegation."""
        # Guard against infinite recursion
        if self._current_depth >= MAX_DELEGATION_DEPTH:
            return {
                "success": False,
                "error": (
                    f"Maximum delegation depth ({MAX_DELEGATION_DEPTH}) reached. "
                    "Cannot spawn further sub-agents. Complete this task directly."
                ),
            }

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

            # Inherit the parent agent's model when no explicit override is given
            effective_model = model or self._parent_model

            # Snapshot the parent's cost so we can report only the sub-agent's
            # incremental spend later (the cost tracker is shared, so a raw read
            # of get_total_cost() would include everything the parent spent).
            parent_cost_before = (
                self._parent_cost_tracker.get_total_cost()
                if getattr(self, "_parent_cost_tracker", None)
                else 0.0
            )

            def _build_sub_agent():
                """Create and fully wire a fresh sub-agent.

                Factored out so the spawn path and the retry path share one
                implementation. Returns ``(agent, persona)``.
                """
                new_agent = Agent(
                    model=effective_model,
                    auto_approve=self._parent_auto_approve,
                    is_subagent=True,
                )

                # Share parent's cost tracker BEFORE reconfiguring delegate
                # tool context — otherwise nested delegations (grand-children)
                # would account against an orphan tracker.
                if self._parent_cost_tracker is not None:
                    new_agent.cost_tracker = self._parent_cost_tracker

                persona = None
                if agent_role:
                    persona = new_agent.set_persona(
                        agent_role,
                        update_model=model is None,
                    )

                # Re-wire the child's delegate_task tool so its
                # _parent_cost_tracker / _parent_model reflect the
                # post-override state. ``set_persona`` already does this
                # when a persona is applied, but not otherwise.
                new_agent._configure_delegate_tool_context()

                # Inherit parent's pinned context.
                if (
                    inherit_project_context
                    and self._parent_context_manager is not None
                ):
                    new_agent.context_manager.pinned_files = dict(
                        self._parent_context_manager.pinned_files
                    )
                    new_agent.context_manager._pinned_mtimes = dict(
                        self._parent_context_manager._pinned_mtimes
                    )
                    if self._parent_context_manager.project_instructions:
                        new_agent.context_manager.project_instructions = (
                            self._parent_context_manager.project_instructions
                        )
                        new_agent.context_manager._instructions_loaded = True
                else:
                    # Explicitly mark as loaded with no content to prevent
                    # the sub-agent from lazily loading them from disk.
                    new_agent.context_manager.project_instructions = None
                    new_agent.context_manager._instructions_loaded = True

                new_agent.create_session()

                new_agent._register_tracker(
                    task=task_description[:120],
                    role=agent_role,
                    parent_id=self._parent_agent_id,
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

