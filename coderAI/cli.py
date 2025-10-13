"""CLI entry point for CoderAI."""

import asyncio
import sys
from pathlib import Path

import click

from . import __version__
from .agent import Agent
from .config import config_manager
from .history import history_manager
from .ui.display import display
from .ui.interactive import interactive_chat


@click.group(invoke_without_command=True)
@click.option("--model", "-m", help="Model to use (gpt-5, gpt-5-mini, gpt-5-nano, lmstudio)")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option("--version", "-v", is_flag=True, help="Show version")
@click.pass_context
def cli(ctx, model, resume, version):
    """CoderAI - Intelligent Coding Agent CLI Tool.

    Run 'coderAI chat' for interactive mode or provide a prompt directly.
    """
    if version:
        click.echo(f"CoderAI version {__version__}")
        sys.exit(0)

    # If no subcommand, default to chat
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat, model=model, resume=resume)


@cli.command()
@click.option("--model", "-m", help="Model to use")
@click.option("--resume", "-r", help="Resume a previous session by ID")
def chat(model, resume):
    """Start an interactive chat session."""
    asyncio.run(_run_chat(model, resume))


async def _run_chat(model, resume):
    """Run interactive chat."""
    try:
        # Create agent
        agent = Agent(model=model, streaming=True)

        # Load or create session
        if resume:
            session = agent.load_session(resume)
            if session:
                display.print_success(f"Resumed session: {resume}")
                display.print_info(f"Messages in history: {len(session.messages)}")
            else:
                display.print_error(f"Session not found: {resume}")
                display.print_info("Starting new session")
                agent.create_session()
        else:
            agent.create_session()

        # Message handler for interactive chat
        async def handle_message(user_input: str, context: dict) -> dict:
            """Handle user message in interactive mode."""
            # Check if awaiting model change
            if context.get("awaiting_model_change"):
                context["awaiting_model_change"] = False
                
                if user_input.lower() == "cancel":
                    display.print_info("Model change cancelled")
                    return {}
                
                # Validate model name
                valid_models = ["gpt-5", "gpt-5-mini", "gpt-5-nano", "lmstudio"]
                if user_input not in valid_models:
                    display.print_error(f"Invalid model: {user_input}")
                    display.print_info(f"Valid models: {', '.join(valid_models)}")
                    return {}
                
                # Change model
                nonlocal agent
                old_model = agent.model
                agent.model = user_input
                agent.provider = agent._create_provider()
                context["model"] = user_input
                
                display.print_success(f"Model changed from {old_model} to {user_input}")
                return {}
            
            response = await agent.process_message(user_input)
            
            # Display assistant response
            if response.get("content"):
                display.print("\n[bold blue]Assistant:[/bold blue]")
                display.print_markdown(response["content"])
                display.print()
            
            return response

        # Run interactive chat
        await interactive_chat.run(
            message_handler=handle_message,
            model=agent.model,
            agent=agent,
            initial_messages=[msg.model_dump() for msg in agent.session.messages] if agent.session else [],
        )

    except KeyboardInterrupt:
        display.print("\n[dim]Goodbye![/dim]")
    except Exception as e:
        display.print_error(f"Fatal error: {str(e)}")
        sys.exit(1)


@cli.command()
@click.argument("prompt")
@click.option("--model", "-m", help="Model to use")
def ask(prompt, model):
    """Ask a single question (single-shot mode)."""
    asyncio.run(_run_single_shot(prompt, model))


async def _run_single_shot(prompt, model):
    """Run single-shot mode."""
    try:
        agent = Agent(model=model, streaming=False)
        
        with display.status("[bold blue]Thinking...[/bold blue]"):
            response = await agent.process_single_shot(prompt)
        
        display.print("\n[bold blue]Response:[/bold blue]")
        display.print_markdown(response)
        display.print()

    except Exception as e:
        display.print_error(f"Error: {str(e)}")
        sys.exit(1)


@cli.group()
def config():
    """Manage configuration."""
    pass


@config.command("show")
def config_show():
    """Show current configuration."""
    config_data = config_manager.show()
    display.print_tree(config_data, "Configuration")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a configuration value."""
    try:
        # Convert value types
        if key in ["temperature"]:
            value = float(value)
        elif key in ["max_tokens", "context_window"]:
            value = int(value)
        elif key in ["streaming", "save_history"]:
            value = value.lower() in ["true", "1", "yes"]

        config_manager.set(key, value)
        display.print_success(f"Set {key} = {value}")
    except Exception as e:
        display.print_error(f"Failed to set config: {str(e)}")


@config.command("reset")
def config_reset():
    """Reset configuration to defaults."""
    config_manager.reset()
    display.print_success("Configuration reset to defaults")


@cli.group()
def history():
    """Manage conversation history."""
    pass


@history.command("list")
def history_list():
    """List all conversation sessions."""
    sessions = history_manager.list_sessions()
    if not sessions:
        display.print_info("No conversation history")
        return

    display.print_table(sessions, title="Conversation Sessions")


@history.command("clear")
@click.confirmation_option(prompt="Are you sure you want to clear all history?")
def history_clear():
    """Clear all conversation history."""
    count = history_manager.clear_history()
    display.print_success(f"Deleted {count} sessions")


@history.command("delete")
@click.argument("session_id")
def history_delete(session_id):
    """Delete a specific session."""
    if history_manager.delete_session(session_id):
        display.print_success(f"Deleted session: {session_id}")
    else:
        display.print_error(f"Session not found: {session_id}")


@cli.command()
@click.option("--model", "-m", help="Model to use")
def info(model):
    """Show information about the agent and model."""
    agent = Agent(model=model)
    model_info = agent.get_model_info()
    
    display.print_header("CoderAI Information")
    display.print(f"\n[bold]Version:[/bold] {__version__}")
    display.print(f"[bold]Config Directory:[/bold] {config_manager.config_dir}")
    display.print(f"[bold]History Directory:[/bold] {history_manager.history_dir}")
    
    display.print("\n[bold cyan]Model Information:[/bold cyan]")
    display.print_tree(model_info, "Current Model")
    
    display.print("\n[bold cyan]Available Tools:[/bold cyan]")
    tools_info = []
    for tool in agent.tools.get_all():
        tools_info.append({
            "Tool": tool.name,
            "Description": tool.description[:60] + "..." if len(tool.description) > 60 else tool.description
        })
    display.print_table(tools_info)


@cli.command()
def setup():
    """Interactive setup wizard."""
    display.print_header("CoderAI Setup Wizard")
    display.print()
    
    # OpenAI API Key
    display.print("[bold]1. OpenAI API Key[/bold]")
    display.print("   Required for using GPT-5 models")
    api_key = click.prompt("   Enter your OpenAI API key (or press Enter to skip)", default="", show_default=False)
    if api_key:
        config_manager.set("openai_api_key", api_key)
        display.print_success("   OpenAI API key saved")
    display.print()
    
    # Default Model
    display.print("[bold]2. Default Model[/bold]")
    display.print("   Available: gpt-5, gpt-5-mini, gpt-5-nano, lmstudio")
    model = click.prompt("   Enter default model", default="gpt-5-mini")
    config_manager.set("default_model", model)
    display.print_success(f"   Default model set to {model}")
    display.print()
    
    # LM Studio (optional)
    display.print("[bold]3. LM Studio Configuration (Optional)[/bold]")
    display.print("   For using local models with LM Studio")
    use_lmstudio = click.confirm("   Configure LM Studio?", default=False)
    if use_lmstudio:
        endpoint = click.prompt("   LM Studio server URL", default="http://localhost:1234/v1")
        config_manager.set("lmstudio_endpoint", endpoint)
        model = click.prompt("   LM Studio model name (optional)", default="local-model", show_default=True)
        config_manager.set("lmstudio_model", model)
        display.print_success("   LM Studio configuration saved")
    display.print()
    
    display.print_success("Setup complete! Run 'coderAI chat' to start.")


@cli.command()
def models():
    """List available models and providers."""
    display.print_header("Available Models and Providers")
    
    display.print("\n[bold cyan]OpenAI Provider[/bold cyan]")
    display.print("  • [yellow]gpt-5[/yellow] - Most capable model")
    display.print("  • [yellow]gpt-5-mini[/yellow] - Balanced performance and cost")
    display.print("  • [yellow]gpt-5-nano[/yellow] - Fast and efficient")
    display.print("\n  [dim]Requires: OpenAI API key[/dim]")
    
    display.print("\n[bold cyan]LM Studio Provider[/bold cyan]")
    display.print("  • [yellow]lmstudio[/yellow] - Use any local model")
    display.print("\n  [dim]Requires: LM Studio running locally[/dim]")
    
    config = config_manager.load()
    display.print(f"\n[bold]Current default:[/bold] [yellow]{config.default_model}[/yellow]")
    display.print()


@cli.command()
@click.argument("model_name")
def set_model(model_name):
    """Set default model for new sessions."""
    valid_models = ["gpt-5", "gpt-5-mini", "gpt-5-nano", "lmstudio"]
    
    if model_name not in valid_models:
        display.print_error(f"Invalid model: {model_name}")
        display.print_info(f"Valid models: {', '.join(valid_models)}")
        display.print_info("Run 'coderAI models' to see all available models")
        return
    
    config_manager.set("default_model", model_name)
    display.print_success(f"Default model set to: {model_name}")


@cli.command()
def status():
    """Show system status and diagnostics."""
    display.print_header("CoderAI System Status")
    
    # Check configuration
    config = config_manager.load()
    display.print("\n[bold cyan]Configuration:[/bold cyan]")
    display.print(f"  Config dir: {config_manager.config_dir}")
    display.print(f"  Default model: [yellow]{config.default_model}[/yellow]")
    display.print(f"  Streaming: {config.streaming}")
    display.print(f"  Save history: {config.save_history}")
    
    # Check OpenAI
    display.print("\n[bold cyan]OpenAI Provider:[/bold cyan]")
    if config.openai_api_key:
        display.print("  ✓ API key configured")
    else:
        display.print("  ✗ API key not configured")
        display.print("    [dim]Run 'coderAI setup' or 'coderAI config set openai_api_key YOUR_KEY'[/dim]")
    
    # Check LM Studio
    display.print("\n[bold cyan]LM Studio Provider:[/bold cyan]")
    display.print(f"  Endpoint: {config.lmstudio_endpoint}")
    display.print(f"  Model: {config.lmstudio_model}")
    
    # Check history
    from .history import history_manager
    sessions = history_manager.list_sessions()
    display.print("\n[bold cyan]History:[/bold cyan]")
    display.print(f"  History dir: {history_manager.history_dir}")
    display.print(f"  Total sessions: {len(sessions)}")
    
    display.print()


def main():
    """Main entry point."""
    # Check if a prompt was provided as a positional argument (not a subcommand)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and sys.argv[1] not in [
        "chat", "ask", "config", "history", "info", "setup", "models", "set-model", "status"
    ]:
        # Treat as a single-shot prompt
        prompt = " ".join(sys.argv[1:])
        asyncio.run(_run_single_shot(prompt, None))
    else:
        cli()


if __name__ == "__main__":
    main()

