from __future__ import annotations

import argparse

from rich.status import Status

from coderai._version import __version__
from coderai.prompt.sections import TOOL_PRESETS
from coderai.ui.shell.console import console


class ShellStartupProgress:
    """Transient startup status shown while the shell is initializing."""

    def __init__(self, *, enabled: bool | None = None) -> None:
        self._enabled = console.is_terminal if enabled is None else enabled
        self._status: Status | None = None

    def update(self, message: str) -> None:
        if not self._enabled:
            return

        status_message = f"[cyan]{message}[/cyan]"
        if self._status is None:
            self._status = console.status(status_message, spinner="dots")
            self._status.start()
            return

        self._status.update(status_message)

    def stop(self) -> None:
        if self._status is None:
            return

        self._status.stop()
        self._status = None


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="coderai", description="coderai — Autonomous AI pair programming in your terminal."
    )
    parser.add_argument("prompt", nargs="*", help="initial prompt (non-interactive when provided)")
    parser.add_argument(
        "--prompt",
        "-p",
        "--command",
        "-c",
        dest="prompt_flag",
        type=str,
        help="Submit a prompt on launch",
    )
    parser.add_argument("--model", "-m", help="LLM model to use")
    parser.add_argument(
        "--mcp-config",
        dest="mcp_config_jsons",
        action="append",
        default=None,
        help="MCP config JSON string to load (repeatable; highest precedence).",
    )
    parser.add_argument(
        "--exec",
        "-x",
        "-e",
        dest="exec_prompt",
        nargs="?",
        const=True,
        default=None,
        help="Run one prompt non-interactively (requires --prompt / -p or positional prompt)",
    )

    parser.add_argument(
        "--resume",
        "-r",
        "--session",
        "-s",
        nargs="?",
        const=True,
        default=None,
        dest="resume",
        help="Resume a specific session by its ID, prefix, or checkpoint. Use without an ID to show session picker.",
    )
    parser.add_argument(
        "--fork",
        "-f",
        nargs="?",
        const=True,
        default=None,
        dest="fork",
        help="Fork a specific session by its ID. Use without an ID to fork the most recent session.",
    )
    parser.add_argument(
        "--last",
        "-l",
        action="store_true",
        default=False,
        dest="last",
        help="Resume the most recent session for the current project directory.",
    )
    parser.add_argument(
        "--preset",
        dest="preset",
        choices=list(TOOL_PRESETS),
        default=None,
        help="Tool preset: full, core, or shell_edit",
    )
    parser.add_argument(
        "--setup",
        dest="setup",
        action="store_true",
        default=False,
        help="Launch interactive setup wizard to configure API keys, endpoints, and models",
    )
    parser.add_argument(
        "--provider",
        dest="setup_provider",
        help="Provider to configure in setup (e.g. openai, deepseek, gemini, anthropic, openrouter, ollama)",
    )
    parser.add_argument(
        "--key",
        dest="setup_key",
        help="API key to save for the specified provider",
    )
    parser.add_argument(
        "--base-url",
        dest="setup_base_url",
        help="Base URL endpoint to configure (for local or custom providers)",
    )
    parser.add_argument(
        "--setup-model",
        dest="setup_model",
        help="Default model to configure in setup",
    )
    parser.add_argument(
        "--test",
        dest="setup_test",
        action="store_true",
        help="Test connection and authentication with active or specified provider",
    )
    parser.add_argument(
        "--status",
        dest="setup_status",
        action="store_true",
        help="Display provider credentials and configuration status table",
    )
    parser.add_argument(
        "--project",
        dest="setup_project",
        action="store_true",
        help="Save configuration to project workspace instead of user global",
    )
    parser.add_argument(
        "--global",
        dest="setup_global",
        action="store_true",
        help="Save configuration to user global settings (~/.coderai)",
    )
    parser.add_argument("--plan", action="store_true", help="start session in Plan Mode")
    # run-mode flags
    parser.add_argument(
        "--continue",
        "-C",
        dest="continue_session",
        action="store_true",
        default=False,
        help="Continue the previous session for the working directory.",
    )
    parser.add_argument(
        "--afk",
        action="store_true",
        default=False,
        help="Run in afk mode: AskUserQuestion auto-dismissed, tool calls auto-approved.",
    )
    parser.add_argument(
        "--print",
        dest="print_mode",
        action="store_true",
        default=False,
        help="Run non-interactively (implies afk for this invocation).",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Alias for --print with minimal output (final message only).",
    )
    parser.add_argument(
        "--final-message-only",
        dest="final_message_only",
        action="store_true",
        default=False,
        help="Only print the final assistant message (requires --print).",
    )
    parser.add_argument(
        "--yolo",
        "--yes",
        "-y",
        "--auto-approve",
        dest="yes",
        action="store_true",
        default=False,
        help="Automatically approve all actions, including file writes and "
        "network access. Only use in a trusted workspace.",
    )
    parser.add_argument(
        "--thinking",
        dest="thinking",
        action="store_true",
        default=None,
        help="Enable thinking mode for this invocation.",
    )
    parser.add_argument(
        "--no-thinking",
        dest="thinking",
        action="store_false",
        help="Disable thinking mode for this invocation.",
    )
    parser.add_argument(
        "--work-dir",
        "-w",
        dest="work_dir",
        default=None,
        help="Working directory for the agent (default: current directory).",
    )
    parser.add_argument(
        "--add-dir",
        dest="add_dirs",
        action="append",
        default=None,
        help="Add an additional directory to the workspace scope (repeatable).",
    )
    parser.add_argument(
        "--skills-dir",
        dest="skills_dirs",
        action="append",
        default=None,
        help="Custom skills directories (repeatable, overrides default discovery).",
    )
    parser.add_argument(
        "--max-steps-per-turn",
        type=int,
        default=None,
        help="Maximum number of steps in one turn.",
    )
    parser.add_argument(
        "--max-retries-per-step",
        type=int,
        default=None,
        help="Maximum number of retries in one step.",
    )
    parser.add_argument(
        "--max-ralph-iterations",
        type=int,
        default=None,
        help="Extra iterations after the first turn in Ralph mode (-1 for unlimited).",
    )
    parser.add_argument(
        "--input-format",
        choices=["text", "stream-json"],
        default=None,
        help="Input format (only with --print reading from stdin).",
    )
    parser.add_argument(
        "--wire",
        dest="wire",
        action="store_true",
        default=False,
        help="Serve the wire protocol (JSON-RPC) on stdio for IDE/headless clients.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "stream-json", "json"],
        default=None,
        help="Output format (only with --print).",
    )
    parser.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="Builtin agent specification to use (e.g. default).",
    )
    parser.add_argument(
        "--agent-file",
        dest="agent_file",
        default=None,
        help="Custom agent specification file.",
    )
    parser.add_argument(
        "--mcp-config-file",
        dest="mcp_config_files",
        action="append",
        default=None,
        help="MCP config file to load (repeatable).",
    )
    parser.add_argument(
        "--config",
        dest="config_string",
        default=None,
        help="Config JSON string to load (overrides files).",
    )
    parser.add_argument(
        "--config-file",
        dest="config_file",
        default=None,
        help="Config file to load instead of ~/.coderai/settings.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Log debug information.",
    )
    parser.add_argument(
        "--subagent-worker",
        action="store_true",
        default=False,
        dest="subagent_worker",
        help="Run as an internal subagent worker process.",
    )
    parser.add_argument(
        "--subagent-payload",
        type=str,
        default=None,
        dest="subagent_payload",
        help="Hex-encoded JSON payload for subagent worker execution.",
    )
    parser.add_argument(
        "--subagent-depth",
        type=int,
        default=0,
        dest="subagent_depth",
        help="Nesting depth of this subagent invocation.",
    )
    parser.add_argument(
        "--subagent-parent-id",
        type=str,
        default=None,
        dest="subagent_parent_id",
        help="Parent agent ID for this subagent execution.",
    )
    parser.add_argument(
        "--subagent-runner",
        choices=["wire", "acp"],
        default="wire",
        dest="subagent_runner",
        help="Runner backend for subagents (wire or acp).",
    )
    parser.add_argument(
        "--subagent-type",
        type=str,
        default=None,
        dest="subagent_type",
        help="Role name or specification for subagent (e.g. general, bash, planner).",
    )
    parser.add_argument(
        "--subagent-desc",
        type=str,
        default="",
        dest="subagent_desc",
        help="Description of the delegated task for the subagent.",
    )
    parser.add_argument(
        "--subagent-allowed-tools",
        type=str,
        default=None,
        dest="subagent_allowed_tools",
        help="Comma-separated list of tool names allowed for this subagent.",
    )
    parser.add_argument(
        "--tools-preset",
        dest="tools_preset",
        choices=list(TOOL_PRESETS),
        default=None,
        help="Tool preset: full, core, or shell_edit (alias: --permission, --preset).",
    )
    parser.add_argument(
        "--permission",
        dest="permission",
        choices=list(TOOL_PRESETS),
        default=None,
        help="Alias for --tools-preset.",
    )
    parser.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        choices=["high", "medium", "low", "off", "xhigh", "max"],
        default=None,
        help="Reasoning effort level for thinking-capable models.",
    )
    parser.add_argument(
        "--max-subagent-depth",
        type=int,
        default=None,
        help="maximum sub-agent nesting depth (default 3; env CODERAI_MAX_SUBAGENT_DEPTH)",
    )
    parser.add_argument(
        "--subagent-timeout",
        type=float,
        default=None,
        help="sub-agent timeout in seconds (default 90; env CODERAI_SUBAGENT_TIMEOUT_SECONDS)",
    )
    parser.add_argument(
        "--max-continuable-agents",
        type=int,
        default=None,
        help="maximum live continuable sub-agents per session (default 50; env CODERAI_MAX_CONTINUABLE_AGENTS_PER_SESSION)",
    )
    parser.add_argument(
        "--max-running-jobs",
        type=int,
        default=None,
        help="maximum concurrent running background jobs per session (default 50; env CODERAI_MAX_RUNNING_JOBS_PER_SESSION)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print debug information")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser
