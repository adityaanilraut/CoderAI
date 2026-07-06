"""Headless / non-interactive one-shot command: ``coderAI run``.

Runs a single prompt through the agent and exits — for CI, scripting, git
hooks, piping, and evals — without launching the Textual TUI. Drives the same
``Agent``/``ExecutionLoop`` core as ``chat``, but with no UIBridge and no
streaming, so stdout receives one clean final answer (or ``--json``).

Safety: with no TTY to confirm mutations, the default is deny-on-mutate. A run
that needs a mutating tool is blocked cleanly (non-zero exit, stderr hint).
Pass ``--yolo``/``--auto-approve`` to allow mutations.
"""

import asyncio
import json as _json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import click

from .utils import missing_api_key_message

logger = logging.getLogger(__name__)


def _resolve_prompt(prompt: Optional[str]) -> Optional[str]:
    """Resolve the prompt text from the arg or piped stdin.

    Returns the prompt string, or ``None`` when none is available (caller
    should exit 2). Reads stdin when the prompt is omitted and stdin is piped,
    or when the prompt is the explicit ``-`` sentinel.
    """
    if prompt is not None and prompt != "-":
        return prompt
    # Explicit "-" or omitted-with-pipe → read stdin.
    if prompt == "-" or not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        return data or None
    return None


def _build_agent(
    *,
    model: Optional[str],
    persona: Optional[str],
    auto_approve: bool,
    resume: Optional[str],
    resume_latest: bool,
    max_iterations: Optional[int],
) -> Any:
    """Construct and prepare an Agent for a one-shot run (no UIBridge).

    A one-shot run has no TTY to recover from a bad resume, so a resume that
    can't be loaded is a hard error (``resume_fresh_on_failure=False``).
    """
    from coderAI.cli.bootstrap import BootstrapError, bootstrap_agent

    try:
        agent = bootstrap_agent(
            model=model,
            resume_id=resume,
            continue_latest=resume_latest,
            streaming=False,
            auto_approve=auto_approve,
            persona=persona,
            resume_fresh_on_failure=False,
        )
    except BootstrapError as e:
        raise click.ClickException(e.message) from e

    if max_iterations is not None:
        agent.config.max_iterations = max_iterations

    return agent


async def _run_agent(agent: Any, prompt: str, blocked_tools: List[str]) -> Dict[str, Any]:
    """Run the agent once and return the result dict, always closing the agent.

    ``blocked_tools`` is populated (in place) with the names of any mutating
    tools denied by the deny-on-mutate guard installed for non-yolo runs.
    """
    if not agent.auto_approve:

        async def _deny_mutations(tool_name: str, _arguments: Dict[str, Any]) -> bool:
            blocked_tools.append(tool_name)
            return False

        agent.confirmation_override = _deny_mutations
        # Phase 5.2: the delegate tool snapshots the confirmation policy when its
        # context is configured (at build time, before this override existed).
        # Re-snapshot now so delegated sub-agents inherit the deny-on-mutate
        # guard and their denied mutations land in ``blocked_tools`` too.
        if hasattr(agent, "_configure_delegate_tool_context"):
            agent._configure_delegate_tool_context()

    try:
        result: Dict[str, Any] = await agent.process_message(prompt)
        return result
    finally:
        await agent.close()


@click.command("run")
@click.argument("prompt", required=False)
@click.option("--model", "-m", help="Model to use")
@click.option("--json", "json_output", is_flag=True, help="Emit a structured JSON result to stdout")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option(
    "--continue",
    "resume_latest",
    is_flag=True,
    help="Resume the most recently updated session",
)
@click.option(
    "--auto-approve",
    "--yolo",
    "auto_approve",
    is_flag=True,
    help="Allow mutating tools to run without confirmation (no deny-on-mutate)",
)
@click.option(
    "--persona",
    "-p",
    default=None,
    help="Persona to load (filename stem in .coderAI/agents/, e.g. 'code-reviewer')",
)
@click.option("--max-iterations", type=int, default=None, help="Override the agent iteration limit")
@click.option(
    "--trust-workspace",
    is_flag=True,
    help="DANGEROUS: trust this workspace's .coderAI hooks/config without a prompt (CI use). "
    "Enables project hooks and the config.json overlay for a cloned repo.",
)
def run(
    prompt: Optional[str],
    model: Optional[str],
    json_output: bool,
    resume: Optional[str],
    resume_latest: bool,
    auto_approve: bool,
    persona: Optional[str],
    max_iterations: Optional[int],
    trust_workspace: bool,
) -> None:
    """Run a single prompt non-interactively and exit (no TUI).

    PROMPT may be passed as an argument, piped via stdin, or read from stdin
    with the ``-`` sentinel:

      coderAI run "refactor utils.py"\n
      echo "list the open TODOs" | coderAI run\n
      coderAI run --json "what is 2+2"
    """
    # Opt this headless run into workspace trust before the agent is built (so
    # the project config overlay applies at construction). Deliberately a
    # per-process env var, not persisted. Default remains untrusted.
    if trust_workspace:
        os.environ["CODERAI_TRUST_WORKSPACE"] = "1"

    if resume and resume_latest:
        _fail("Pass either --resume or --continue, not both.", json_output, exit_code=2)

    resolved = _resolve_prompt(prompt)
    if not resolved:
        _fail(
            "No prompt provided. Pass it as an argument, pipe it via stdin, or use '-'.",
            json_output,
            exit_code=2,
        )
        return  # unreachable; keeps type-checkers happy

    key_error = missing_api_key_message()
    if key_error:
        _fail(key_error, json_output, exit_code=1)

    blocked_tools: List[str] = []
    try:
        agent = _build_agent(
            model=model,
            persona=persona,
            auto_approve=auto_approve,
            resume=resume,
            resume_latest=resume_latest,
            max_iterations=max_iterations,
        )
        result = asyncio.run(_run_agent(agent, resolved, blocked_tools))
    except click.ClickException:
        raise
    except KeyboardInterrupt:
        _fail("Interrupted.", json_output, exit_code=1)
        return
    except Exception as e:  # noqa: BLE001 — surface any agent/runtime failure cleanly
        logger.debug("Headless run failed", exc_info=True)
        _fail(f"Agent run failed: {e}", json_output, exit_code=1)
        return

    response = str(result.get("content", "") or "")
    blocked = bool(blocked_tools)
    success = not blocked

    if json_output:
        payload = {
            "response": response,
            "success": success,
            "session_id": getattr(getattr(agent, "session", None), "session_id", None),
            "model": agent.model,
            "cost_usd": agent.cost_tracker.get_total_cost(),
            "blocked_tools": sorted(set(blocked_tools)),
        }
        click.echo(_json.dumps(payload))
    else:
        if response:
            click.echo(response)
        if blocked:
            names = ", ".join(sorted(set(blocked_tools)))
            click.echo(
                f"\nBlocked mutating tool call(s): {names}. Re-run with --yolo to allow.",
                err=True,
            )

    sys.exit(0 if success else 1)


def _fail(message: str, json_output: bool, *, exit_code: int) -> None:
    """Report a failure and exit. ``--json`` still emits a JSON object."""
    if json_output:
        click.echo(_json.dumps({"response": "", "success": False, "error": message}))
    else:
        click.echo(message, err=True)
    sys.exit(exit_code)
