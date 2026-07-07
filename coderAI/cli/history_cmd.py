"""CLI history subcommands."""

import sys

import click

from coderAI.system.history import history_manager


@click.group(invoke_without_command=True)
@click.pass_context
def history(ctx: click.Context) -> None:
    """Manage conversation history."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@history.command("list")
def history_list() -> None:
    """List all conversation sessions."""
    from coderAI.cli.utils import display

    sessions = history_manager.list_sessions()
    if not sessions:
        display.print_info("No conversation history")
        return
    display.print_table(sessions, title="Conversation Sessions")


@history.command("clear")
@click.confirmation_option(prompt="Are you sure you want to clear all history?")
def history_clear() -> None:
    """Clear all conversation history."""
    from coderAI.cli.utils import display

    count = history_manager.clear_history()
    display.print_success(f"Deleted {count} sessions")


@history.command("delete")
@click.argument("session_id")
def history_delete(session_id: str) -> None:
    """Delete a specific session."""
    from coderAI.cli.utils import display

    if history_manager.delete_session(session_id):
        display.print_success(f"Deleted session: {session_id}")
    else:
        display.print_error(f"Session not found: {session_id}")
        sys.exit(1)
