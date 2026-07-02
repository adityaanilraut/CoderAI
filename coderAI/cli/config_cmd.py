"""CLI config subcommands."""

import sys

import click

from coderAI.system.config import config_manager


@click.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Manage configuration."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config.command("show")
def config_show() -> None:
    """Show current configuration."""
    from coderAI.ui.display import display

    config_data = config_manager.show()
    display.print_tree(config_data, "Configuration")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a configuration value."""
    from coderAI.ui.display import display

    parsed: str | int | float | bool = value
    try:
        if key in ["temperature", "budget_limit", "subagent_timeout_seconds"]:
            parsed = float(value)
        elif key in [
            "max_tokens",
            "context_window",
            "max_iterations",
            "max_tool_output",
            "approval_timeout_seconds",
        ]:
            parsed = int(value)
        elif key in [
            "streaming",
            "save_history",
            "web_tools_in_main",
            "continue_loop_on_deny",
            "allow_outside_project",
        ]:
            parsed = value.lower() in ["true", "1", "yes"]

        config_manager.set(key, parsed)
        display.print_success(f"Set {key} = {parsed}")
    except Exception as e:
        display.print_error(f"Failed to set config: {str(e)}")
        if isinstance(e, ValueError):
            sys.exit(2)
        sys.exit(1)


@config.command("reset")
def config_reset() -> None:
    """Reset configuration to defaults."""
    from coderAI.ui.display import display

    config_manager.reset()
    display.print_success("Configuration reset to defaults")
