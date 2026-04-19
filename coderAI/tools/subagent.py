"""Sub-agent delegation tool for multi-agent capabilities."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Tool
from ..events import event_emitter
from ..context_selector import extract_keywords, extract_relevant_snippets

logger = logging.getLogger(__name__)

# Maximum depth for nested sub-agent delegation to prevent infinite recursion
MAX_DELEGATION_DEPTH = 3

# Role-specific system prompt fragments that give concrete guidance per specialty.
_ROLE_INSTRUCTIONS: Dict[str, str] = {
    "code reviewer": (
        "Focus on: code correctness, edge cases, error handling, naming clarity, "
        "performance pitfalls, and adherence to project conventions. "
        "Structure your report as: Summary → Critical Issues → Suggestions → Verdict."
    ),
    "security expert": (
        "Focus on: injection vulnerabilities, authentication/authorization flaws, "
        "secrets exposure, dependency vulnerabilities, and insecure defaults. "
        "Rate each finding by severity (Critical / High / Medium / Low)."
    ),
    "senior qa engineer": (
        "Focus on: test coverage gaps, missing edge-case tests, flaky test patterns, "
        "and test architecture improvements. Suggest concrete test cases to add."
    ),
    "data scientist": (
        "Focus on: data pipeline correctness, statistical validity, performance of "
        "data transformations, and visualization clarity."
    ),
    "devops engineer": (
        "Focus on: CI/CD configuration, deployment safety, infrastructure-as-code "
        "correctness, container best practices, and monitoring gaps."
    ),
    "technical writer": (
        "Focus on: documentation accuracy, completeness, clarity, and consistency. "
        "Check docstrings, README, API docs, and inline comments."
    ),
}


def _get_role_instructions(role: Optional[str]) -> str:
    """Get role-specific instructions.

    Falls back to the hardcoded `_ROLE_INSTRUCTIONS`, or a generic prompt.
    File-backed personas are applied directly to the sub-agent elsewhere.
    """
    if not role:
        return ""

    # Fallback to hardcoded roles
    role_lower = role.lower().strip()
    for key, instructions in _ROLE_INSTRUCTIONS.items():
        if key in role_lower:
            return instructions

    # Generic fallback for unknown roles
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
        "specific file paths and expected output format for best results."
    )
    parameters_model = DelegateTaskParams
    # Sub-agents may run mutating tools; never run delegations in parallel with
    # other tools (avoids branch-restore races and shared tracker/cost churn).
    is_read_only = False

    # Set by the parent Agent after registration so the sub-agent can
    # inherit the model and link to the correct parent in the tracker.
    _parent_model: Optional[str] = None
    _parent_agent_id: Optional[str] = None
    _parent_context_manager: Optional[Any] = None  # parent's ContextManager
    _parent_cost_tracker: Optional[Any] = None  # parent's CostTracker (shared)
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
        branch_before: Optional[str] = None
        try:
            from ..agent import Agent

            # Capture current branch for restoration after sub-agent completes
            from ..safeguards import get_current_branch
            cwd = os.getcwd()
            branch_before = await get_current_branch(cwd)
            logger.info(
                f"Sub-agent delegation: branch_before={branch_before} "
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
                    model=effective_model, auto_approve=True, is_subagent=True
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
                "- Do NOT parse HTML or scrape web pages using complex shell pipelines (`curl | grep | sed`). Use proper tools (`read_url`, `web_search`) or Python scripts instead.",
                f"- IMPORTANT: You are on branch '{branch_before or 'unknown'}'. Do NOT switch branches unless explicitly required by the task.",
            ]

            if role_instructions:
                system_preamble_parts.extend(["", f"ROLE-SPECIFIC GUIDANCE ({agent_role}):", role_instructions])

            system_preamble = "\n".join(system_preamble_parts)

            # Prepend to the sub-agent's existing system prompt
            for msg in sub_agent.session.messages:
                if msg.role == "system":
                    msg.content = f"{system_preamble}\n\n---\n\n{msg.content}"
                    break

            # Add context hints to the task description instead of the system prompt
            context_parts = []
            if context_hints:
                context_parts.extend(["CONTEXT PROVIDED BY PARENT AGENT:"])
                keywords = extract_keywords(task_description)

                async def _load_hint(hint: str) -> str:
                    hint_path = Path(hint)
                    if hint_path.is_file():
                        try:
                            content = await asyncio.to_thread(
                                hint_path.read_text, encoding="utf-8", errors="replace"
                            )
                            snippet = extract_relevant_snippets(content, keywords, max_lines=80)
                            return f"\n### File: {hint}\n```\n{snippet}\n```"
                        except Exception:
                            return f"  - {hint}"
                    return f"  - {hint}"

                hint_parts = await asyncio.gather(*(_load_hint(h) for h in context_hints))
                context_parts.extend(hint_parts)

            if context_parts:
                task_description = "\n".join(context_parts) + "\n\n---\n\nTASK DESCRIPTION:\n" + task_description

            # Process the task with retry logic
            max_retries = 2
            last_error = None
            final_report = None

            for attempt in range(1, max_retries + 2):  # 1 initial + 2 retries
                try:
                    truncated_desc = task_description[:80] + ("..." if len(task_description) > 80 else "")
                    attempt_label = f" (attempt {attempt})" if attempt > 1 else ""
                    event_emitter.emit(
                        "agent_status",
                        message=f"[dim]Sub-Agent working on{attempt_label}: {truncated_desc}[/dim]",
                    )

                    final_report = await sub_agent.process_single_shot(task_description)
                    last_error = None
                    break  # Success — exit retry loop

                except Exception as retry_err:
                    last_error = retry_err
                    if attempt <= max_retries:
                        wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s
                        event_emitter.emit(
                            "agent_status",
                            message=(
                                f"[yellow]Sub-Agent attempt {attempt} failed: {str(retry_err)[:80]}. "
                                f"Retrying in {wait_time}s...[/yellow]"
                            ),
                        )
                        await asyncio.sleep(wait_time)

                        # Tear down the failed sub-agent: mark its tracker
                        # entry as errored (so `agent_tracker.get_active()`
                        # doesn't report phantom "thinking" agents) and
                        # release HTTP/subprocess resources.
                        try:
                            sub_agent._finish_tracker(error=True)
                        except Exception as tracker_err:
                            logger.warning(
                                f"Error finishing stale sub-agent tracker: {tracker_err}"
                            )
                        try:
                            await sub_agent.close()
                        except Exception as close_err:
                            logger.warning(f"Error closing sub-agent during retry cleanup: {close_err}")

                        # Rebuild a fresh sub-agent through the same helper
                        sub_agent, applied_persona = _build_sub_agent()
                        for msg in sub_agent.session.messages:
                            if msg.role == "system":
                                msg.content = f"{system_preamble}\n\n---\n\n{msg.content}"
                                break
                    else:
                        logger.error(f"Sub-agent failed after {max_retries + 1} attempts: {retry_err}")

            # If all retries failed
            if last_error is not None:
                raise last_error

            tokens_used = sub_agent.total_tokens
            # Report only the delta attributable to this sub-agent. The cost
            # tracker is shared with the parent, so subtracting the pre-spawn
            # snapshot gives the sub-agent's incremental spend.
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

            # Restore original branch if the sub-agent changed it
            if branch_before is not None:
                try:
                    from ..safeguards import get_current_branch
                    branch_after = await get_current_branch(os.getcwd())
                    if branch_after != branch_before:
                        logger.warning(
                            f"Sub-agent changed branch from '{branch_before}' to "
                            f"'{branch_after}'. Restoring original branch."
                        )
                        process = await asyncio.create_subprocess_exec(
                            "git", "checkout", branch_before,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=os.getcwd(),
                        )
                        await process.communicate()
                        if process.returncode == 0:
                            logger.info(f"Restored branch to '{branch_before}'")
                        else:
                            logger.error(
                                f"Failed to restore branch to '{branch_before}'"
                            )
                except Exception as branch_err:
                    logger.error(f"Error restoring branch: {branch_err}")


