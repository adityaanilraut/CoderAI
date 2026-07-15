"""CLI history subcommands."""

import json
import sys
from typing import Optional

import click

from coderAI.system.history import Session, history_manager


def _session_to_markdown(session: Session) -> str:
    """Render the complete persisted session, including tool metadata."""
    title = session.name or session.session_id
    session_data = session.model_dump(mode="json")
    messages = session_data.pop("messages")
    lines = [f"# {title}", "", "## Session", "", "```json"]
    lines.extend(json.dumps(session_data, indent=2, ensure_ascii=False).splitlines())
    lines.extend(["```", ""])
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role", "message")).title()
        lines.extend([f"## {index}. {role}", "", "```json"])
        lines.extend(json.dumps(message, indent=2, ensure_ascii=False).splitlines())
        lines.extend(["```", ""])
    return "\n".join(lines)


def _load_session_or_exit(session_id: str) -> Session:
    session = history_manager.load_session(session_id)
    if session is None:
        click.echo(f"Session not found: {session_id}", err=True)
        raise click.exceptions.Exit(1)
    return session


@click.group(invoke_without_command=True)
@click.pass_context
def history(ctx: click.Context) -> None:
    """Manage conversation history."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@history.command("list")
@click.option("--tag", help="Only show sessions with this tag")
@click.option("--filter", "query", help="Filter by session ID, name, or tag")
def history_list(tag: Optional[str], query: Optional[str]) -> None:
    """List all conversation sessions."""
    from coderAI.cli.utils import display

    sessions = history_manager.list_sessions(tag=tag, query=query)
    if not sessions:
        display.print_info("No conversation history")
        return
    rows = [{**session, "tags": ", ".join(session.get("tags", []))} for session in sessions]
    display.print_table(rows, title="Conversation Sessions")


@history.command("rename")
@click.argument("session_id")
@click.argument("name", required=False)
@click.option("--clear", "clear_name", is_flag=True, help="Remove the current session name")
def history_rename(session_id: str, name: Optional[str], clear_name: bool) -> None:
    """Set a session display name, or remove it with --clear."""
    if clear_name and name:
        raise click.UsageError("Pass a name or --clear, not both.")
    if not clear_name and not name:
        raise click.UsageError("Pass a name or use --clear.")
    if not history_manager.rename_session(session_id, None if clear_name else name):
        click.echo(f"Session not found: {session_id}", err=True)
        raise click.exceptions.Exit(1)
    click.echo(f"Renamed session: {session_id}" if name else f"Cleared name: {session_id}")


@history.command("tag")
@click.argument("session_id")
@click.argument("tags", nargs=-1)
@click.option("--remove", is_flag=True, help="Remove tags instead of adding them")
@click.option("--clear", "clear_tags", is_flag=True, help="Remove all tags")
def history_tag(session_id: str, tags: tuple[str, ...], remove: bool, clear_tags: bool) -> None:
    """Add tags to a session; use --remove or --clear to remove them."""
    if clear_tags and (tags or remove):
        raise click.UsageError("Use --clear by itself.")
    if not clear_tags and not tags:
        raise click.UsageError("Pass at least one tag or use --clear.")
    updated = (
        history_manager.set_session_tags(session_id, [])
        if clear_tags
        else history_manager.tag_session(session_id, list(tags), remove=remove)
    )
    if not updated:
        click.echo(f"Session not found: {session_id}", err=True)
        raise click.exceptions.Exit(1)
    action = "Cleared tags for" if clear_tags else "Updated tags for"
    click.echo(f"{action}: {session_id}")


@history.command("export")
@click.argument("session_id")
@click.option(
    "--format",
    "export_format",
    type=click.Choice(["markdown", "json"], case_sensitive=False),
    default="markdown",
    show_default=True,
)
def history_export(session_id: str, export_format: str) -> None:
    """Write a complete persisted transcript to stdout."""
    session = _load_session_or_exit(session_id)
    if export_format.lower() == "json":
        click.echo(json.dumps(session.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        click.echo(_session_to_markdown(session))


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
