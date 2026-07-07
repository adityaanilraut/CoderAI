"""CLI tasks subcommands."""

import asyncio
import sys

import click


@click.group(invoke_without_command=True)
@click.pass_context
def tasks(ctx: click.Context) -> None:
    """Manage project tasks and TODOs."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@tasks.command("list")
def tasks_list() -> None:
    """List all tasks."""
    from coderAI.tools.tasks import ManageTasksTool
    from coderAI.cli.utils import display

    tool = ManageTasksTool()
    result = asyncio.run(tool.execute("list"))
    if not result.get("success"):
        display.print_error(result.get("error", "Unknown error"))
        sys.exit(1)

    display.print_header(result.get("summary", "Tasks"))

    for status, color in [("pending", "yellow"), ("completed", "green")]:
        task_list = result.get(status, [])
        if task_list:
            display.print(f"\n[bold {color}]{status.title()} Tasks:[/bold {color}]")
            for t in task_list:
                desc = f" - {t['description']}" if t.get("description") else ""
                display.print(f"  [{t['id']}] {t['title']}{desc}")
