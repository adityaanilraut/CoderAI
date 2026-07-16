"""Headless / non-interactive one-shot command: ``coderAI run``.

Runs a single prompt through the agent and exits — for CI, scripting, git
hooks, piping, and evals — without launching the Textual TUI. Drives the same
``Agent``/``ExecutionLoop`` core as ``chat``, but with no UIBridge and no
interactive prompts. Stdout is reserved for the selected text, JSON, or NDJSON
output format.

Safety: with no TTY to confirm mutations, the default is deny-on-mutate. A run
that needs a mutating tool is blocked cleanly (non-zero exit, stderr hint).
Pass ``--yolo``/``--auto-approve`` to allow mutations.
"""

import asyncio
import json as _json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import click

from .utils import missing_api_key_message

logger = logging.getLogger(__name__)

NDJSON_SCHEMA_VERSION = 1


def _json_safe(value: Any) -> Any:
    """Convert event payload values to JSON-safe data without private state."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
        }
    return str(value)


class _NDJSONEventStream:
    """Serialize core events as ordered schema-versioned stdout envelopes."""

    _EVENT_TYPES = {
        "tool_call": "tool.started",
        "tool_result": "tool.completed",
        "tool_error": "tool.error",
        "tool_progress": "tool.progress",
        "agent_status": "agent.status",
        "agent_warning": "agent.warning",
        "agent_error": "agent.error",
        "agent_paused": "agent.paused",
        "agent_lifecycle": "agent.lifecycle",
        "agent_tracker_sync": "agent.update",
        "file_diff": "file.diff",
        "tasks_update": "tasks.updated",
    }

    def __init__(self) -> None:
        self._sequence = 0
        self._terminal = False
        self._lock = threading.Lock()
        self._listeners: List[Tuple[str, Callable[..., None]]] = []

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Write one non-terminal envelope unless a terminal event was sent."""
        self._write(event_type, data or {}, terminal=False)

    def finish(self, event_type: str, data: Dict[str, Any]) -> None:
        """Write the stream's exactly-once terminal result or error envelope."""
        self._write(event_type, data, terminal=True)

    def _write(self, event_type: str, data: Dict[str, Any], *, terminal: bool) -> None:
        with self._lock:
            if self._terminal:
                return
            if terminal:
                self._terminal = True
            self._sequence += 1
            envelope = {
                "schema_version": NDJSON_SCHEMA_VERSION,
                "sequence": self._sequence,
                "timestamp": time.time(),
                "type": event_type,
                "terminal": terminal,
                "data": _json_safe(data),
            }
            click.echo(
                _json.dumps(
                    envelope,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )

    def subscribe(self) -> None:
        """Subscribe synchronous serializers to the process-wide core event bus."""
        from coderAI.system.events import event_emitter

        for event_name, event_type in self._EVENT_TYPES.items():

            def callback(
                *args: Any,
                _event_type: str = event_type,
                **kwargs: Any,
            ) -> None:
                payload = dict(kwargs)
                if args:
                    payload["args"] = list(args)
                self.emit(_event_type, payload)

            event_emitter.on(event_name, callback)
            self._listeners.append((event_name, callback))

    def unsubscribe(self) -> None:
        """Remove all event listeners so repeated in-process CLI runs stay isolated."""
        from coderAI.system.events import event_emitter

        for event_name, callback in self._listeners:
            event_emitter.off(event_name, callback)
        self._listeners.clear()


class _NDJSONStreamAdapter:
    """Translate the existing provider stream handler's turn events to NDJSON."""

    def __init__(self, stream: _NDJSONEventStream) -> None:
        self.stream = stream

    def emit(self, event: str, **data: Any) -> None:
        if event != "turn":
            self.stream.emit(event, data)
            return
        phase = str(data.pop("phase", "update"))
        event_type = {
            "start": "assistant.started",
            "text": "assistant.delta",
            "reasoning": "assistant.reasoning_delta",
            "end": "assistant.completed",
        }.get(phase, "assistant.update")
        self.stream.emit(event_type, data)


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
    streaming: bool = False,
) -> Any:
    """Construct and prepare an Agent for a one-shot run (no UIBridge).

    A one-shot run has no TTY to recover from a bad resume, so a resume that
    can't be loaded is a hard error (``resume_fresh_on_failure=False``).
    """
    from coderAI.core.session_bootstrap import BootstrapError, bootstrap_agent

    try:
        agent = bootstrap_agent(
            model=model,
            resume_id=resume,
            continue_latest=resume_latest,
            streaming=streaming,
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
@click.option(
    "--output",
    type=click.Choice(["text", "json", "ndjson"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Select stdout format",
)
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
    output: str,
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
    output_mode = "json" if json_output else output.lower()
    event_stream = _NDJSONEventStream() if output_mode == "ndjson" else None

    # Opt this headless run into workspace trust before the agent is built (so
    # the project config overlay applies at construction). Deliberately a
    # per-process env var, not persisted. Default remains untrusted.
    if trust_workspace:
        os.environ["CODERAI_TRUST_WORKSPACE"] = "1"

    if resume and resume_latest:
        _fail(
            "Pass either --resume or --continue, not both.",
            output_mode,
            event_stream,
            exit_code=2,
        )

    resolved = _resolve_prompt(prompt)
    if not resolved:
        _fail(
            "No prompt provided. Pass it as an argument, pipe it via stdin, or use '-'.",
            output_mode,
            event_stream,
            exit_code=2,
        )
        return  # unreachable; keeps type-checkers happy

    if event_stream is not None:
        event_stream.subscribe()
        event_stream.emit(
            "run.started",
            {
                "model": model,
                "resume": resume,
                "continue": resume_latest,
            },
        )
    try:
        key_error = missing_api_key_message()
        if key_error:
            _fail(key_error, output_mode, event_stream, exit_code=1)

        blocked_tools: List[str] = []
        agent = _build_agent(
            model=model,
            persona=persona,
            auto_approve=auto_approve,
            resume=resume,
            resume_latest=resume_latest,
            max_iterations=max_iterations,
            streaming=output_mode == "ndjson",
        )
        if event_stream is not None and bool(getattr(agent, "streaming", False)):
            from coderAI.tui.streaming import BridgeStreamingHandler

            stream_server: Any = _NDJSONStreamAdapter(event_stream)
            agent.streaming_handler = BridgeStreamingHandler(stream_server)
        result = asyncio.run(_run_agent(agent, resolved, blocked_tools))
    except click.ClickException as e:
        _fail(str(e), output_mode, event_stream, exit_code=e.exit_code)
        return
    except KeyboardInterrupt:
        _fail("Interrupted.", output_mode, event_stream, exit_code=1)
        return
    except Exception as e:  # noqa: BLE001 — surface any agent/runtime failure cleanly
        logger.debug("Headless run failed", exc_info=True)
        _fail(f"Agent run failed: {e}", output_mode, event_stream, exit_code=1)
        return
    finally:
        if event_stream is not None:
            event_stream.unsubscribe()

    response = str(result.get("content", "") or "")
    blocked = bool(blocked_tools)
    success = not blocked
    payload = {
        "response": response,
        "success": success,
        "session_id": getattr(getattr(agent, "session", None), "session_id", None),
        "model": agent.model,
        "cost_usd": agent.cost_tracker.get_total_cost(),
        "blocked_tools": sorted(set(blocked_tools)),
    }

    if output_mode == "json":
        click.echo(_json.dumps(payload))
    elif event_stream is not None:
        event_stream.finish("result", payload)
    elif response:
        click.echo(response)
    if blocked and output_mode != "json":
        names = ", ".join(sorted(set(blocked_tools)))
        click.echo(
            f"\nBlocked mutating tool call(s): {names}. Re-run with --yolo to allow.",
            err=True,
        )

    sys.exit(0 if success else 1)


def _fail(
    message: str,
    output_mode: str,
    event_stream: Optional[_NDJSONEventStream] = None,
    *,
    exit_code: int,
) -> None:
    """Report a failure and exit in the selected stdout format."""
    payload = {"response": "", "success": False, "error": message}
    if output_mode == "json":
        click.echo(_json.dumps({"response": "", "success": False, "error": message}))
    elif output_mode == "ndjson":
        stream = event_stream or _NDJSONEventStream()
        stream.finish("error", payload)
        click.echo(message, err=True)
    else:
        click.echo(message, err=True)
    sys.exit(exit_code)
