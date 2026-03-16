"""CLI entry point for CoderAI."""

import asyncio
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

# Load .env file before anything else reads environment variables
load_dotenv()

from . import __version__
from .agent import Agent
from .config import config_manager
from .history import history_manager
from .ui.display import display
from .ui.interactive import interactive_chat


@click.group(invoke_without_command=True)
@click.option("--model", "-m", help="Model to use (gpt-5, gpt-5-mini, claude-4-sonnet, lmstudio, ollama)")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option("--version", "-v", is_flag=True, help="Show version")
@click.option("--verbose", is_flag=True, help="Enable verbose/debug logging")
@click.pass_context
def cli(ctx, model, resume, version, verbose):
    """CoderAI - Intelligent Coding Agent CLI Tool.

    Run 'coderAI chat' for interactive mode.
    """
    if version:
        click.echo(f"CoderAI version {__version__}")
        sys.exit(0)

    # Set up logging if verbose
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    
    # Store options in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    # If no subcommand, default to chat
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat, model=model, resume=resume)


@cli.command()
@click.option("--model", "-m", help="Model to use")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option("--no-stream", is_flag=True, help="Disable streaming responses")
@click.option("--auto-approve", "--yolo", is_flag=True, help="Skip tool confirmation prompts")
def chat(model, resume, no_stream, auto_approve):
    """Start an interactive chat session."""
    asyncio.run(_run_chat(model, resume, no_stream, auto_approve))


async def _run_chat(model, resume, no_stream=False, auto_approve=False):
    """Run interactive chat."""
    try:
        # Create agent
        streaming = not no_stream
        agent = Agent(model=model, streaming=streaming, auto_approve=auto_approve)

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

        # Auto-context: detect project type and inject summary (F7)
        project_summary = ""
        try:
            from .tools.project import ProjectContextTool
            ctx_tool = ProjectContextTool()
            ctx_result = await ctx_tool.execute(path=".")
            if ctx_result.get("success"):
                proj_type = ctx_result.get("project_type", "unknown")
                file_count = ctx_result.get("file_count", 0)
                deps = ctx_result.get("key_dependencies", [])
                deps_str = ", ".join(deps[:8]) if deps else "none detected"
                project_summary = f"📁 {proj_type} project | {file_count} files | {deps_str}"
                # Inject a lightweight context note into the system prompt
                if agent.session and agent.session.messages:
                    agent.session.add_message(
                        "system",
                        f"[Project context] Type: {proj_type}. "
                        f"Key deps: {deps_str}. Files: {file_count}.",
                    )
        except Exception:
            pass  # Non-critical — don't block startup

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
                valid_models = list(agent.provider.SUPPORTED_MODELS.keys()) if hasattr(agent.provider, 'SUPPORTED_MODELS') else []
                valid_models.extend(["lmstudio", "ollama", "claude-4-sonnet", "claude-3.5-sonnet", "claude-3.5-haiku", "claude-3-opus", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama3-70b-8192", "llama3-8b-8192", "deepseek-v3", "deepseek-v3.2", "deepseek-r1", "deepseek-chat", "deepseek-reasoner"])
                
                if user_input not in valid_models:
                    display.print_error(f"Invalid model: {user_input}")
                    display.print_info(f"Valid models: {', '.join(valid_models)}")
                    return {}
                
                # Change model
                old_model = agent.model
                agent.model = user_input
                agent.provider = agent._create_provider()
                context["model"] = user_input
                
                display.print_success(f"Model changed from {old_model} to {user_input}")
                return {}
            
            # Check if awaiting reasoning change
            if context.get("awaiting_reasoning_change"):
                context["awaiting_reasoning_change"] = False
                
                if user_input.lower() == "cancel":
                    display.print_info("Reasoning change cancelled")
                    return {}
                    
                valid_efforts = ["high", "medium", "low", "none"]
                
                if user_input.lower() not in valid_efforts:
                    display.print_error(f"Invalid reasoning effort: {user_input}")
                    display.print_info(f"Valid options: {', '.join(valid_efforts)}")
                    return {}
                    
                # Change reasoning effort
                old_effort = getattr(agent.config, "reasoning_effort", "medium")
                config_manager.set("reasoning_effort", user_input.lower())
                agent.config = config_manager.load()
                agent.provider = agent._create_provider()  # Re-init provider with new config
                context["reasoning_effort"] = user_input.lower()
                
                display.print_success(f"Reasoning effort changed from {old_effort} to {user_input.lower()}")
                return {}
            
            response = await agent.process_message(user_input)
            
            # Display assistant response only if we didn't already stream it
            if response.get("content") and not getattr(agent, "streaming", True):
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
    finally:
        try:
            await agent.close()
        except NameError:
            pass  # agent was never assigned



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
        elif key in ["max_tokens", "context_window", "max_iterations", "max_tool_output"]:
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
    display.print("   Required for using GPT models")
    api_key = click.prompt("   Enter your OpenAI API key (or press Enter to skip)", default="", show_default=False)
    if api_key:
        config_manager.set("openai_api_key", api_key)
        display.print_success("   OpenAI API key saved")
    display.print()
    
    # Anthropic API Key
    display.print("[bold]2. Anthropic API Key[/bold]")
    display.print("   Required for using Claude models")
    anthropic_key = click.prompt("   Enter your Anthropic API key (or press Enter to skip)", default="", show_default=False)
    if anthropic_key:
        config_manager.set("anthropic_api_key", anthropic_key)
        display.print_success("   Anthropic API key saved")
    display.print()
    
    # Groq API Key
    display.print("[bold]3. Groq API Key[/bold]")
    display.print("   Required for using Groq models (including openai/gpt-oss-120b and openai/gpt-oss-20b)")
    groq_key = click.prompt("   Enter your Groq API key (or press Enter to skip)", default="", show_default=False)
    if groq_key:
        config_manager.set("groq_api_key", groq_key)
        display.print_success("   Groq API key saved")
    display.print()
    
    # DeepSeek API Key
    display.print("[bold]4. DeepSeek API Key[/bold]")
    display.print("   Required for using DeepSeek models")
    deepseek_key = click.prompt("   Enter your DeepSeek API key (or press Enter to skip)", default="", show_default=False)
    if deepseek_key:
        config_manager.set("deepseek_api_key", deepseek_key)
        display.print_success("   DeepSeek API key saved")
    display.print()
    
    # Default Model
    display.print("[bold]5. Default Model[/bold]")
    display.print("   Available: gpt-5, gpt-5-mini, claude-4-sonnet, claude-3.5-sonnet, lmstudio, ollama, openai/gpt-oss-120b, openai/gpt-oss-20b, deepseek-v3.2")
    model = click.prompt("   Enter default model", default="gpt-5-mini")
    config_manager.set("default_model", model)
    display.print_success(f"   Default model set to {model}")
    display.print()
    
    # Reasoning Effort
    display.print("[bold]6. Reasoning Effort[/bold]")
    display.print("   How much thinking reasoning models (e.g. o1, o3-mini, claude-3.7-sonnet) should use.")
    display.print("   Available: high, medium, low, none")
    effort = click.prompt("   Enter reasoning effort", default="medium")
    config_manager.set("reasoning_effort", effort.lower())
    display.print_success(f"   Reasoning effort set to {effort.lower()}")
    display.print()
    
    # LM Studio (optional)
    display.print("[bold]7. LM Studio Configuration (Optional)[/bold]")
    display.print("   For using local models with LM Studio")
    use_lmstudio = click.confirm("   Configure LM Studio?", default=False)
    if use_lmstudio:
        endpoint = click.prompt("   LM Studio server URL", default="http://localhost:1234/v1")
        config_manager.set("lmstudio_endpoint", endpoint)
        model = click.prompt("   LM Studio model name (optional)", default="local-model", show_default=True)
        config_manager.set("lmstudio_model", model)
        display.print_success("   LM Studio configuration saved")
    display.print()

    # Ollama (optional)
    display.print("[bold]8. Ollama Configuration (Optional)[/bold]")
    display.print("   For using local models with Ollama")
    use_ollama = click.confirm("   Configure Ollama?", default=False)
    if use_ollama:
        endpoint = click.prompt("   Ollama server URL", default="http://localhost:11434/v1")
        config_manager.set("ollama_endpoint", endpoint)
        model = click.prompt("   Ollama model name", default="llama3", show_default=True)
        config_manager.set("ollama_model", model)
        display.print_success("   Ollama configuration saved")
    display.print()
    
    display.print_success("Setup complete! Run 'coderAI chat' to start.")


@cli.command()
def models():
    """List available models and providers."""
    display.print_header("Available Models and Providers")
    
    display.print("\n[bold cyan]OpenAI Provider[/bold cyan]")
    display.print("  • [yellow]gpt-5[/yellow] - GPT-5 (multimodal)")
    display.print("  • [yellow]gpt-5-mini[/yellow] - GPT-5 Mini (fastest and most affordable GPT-5 option)")
    display.print("  • [yellow]gpt-5-nano[/yellow] - GPT-5 Nano (lowest cost, quickest)")
    display.print("  • [yellow]o1[/yellow] - o1 (reasoning model)")
    display.print("  • [yellow]o1-mini[/yellow] - o1 Mini (fast reasoning)")
    display.print("  • [yellow]o3-mini[/yellow] - o3 Mini (latest reasoning)")
    display.print("\n  [dim]Requires: OpenAI API key[/dim]")
    
    display.print("\n[bold cyan]Anthropic Provider[/bold cyan]")
    display.print("  • [yellow]claude-4-sonnet[/yellow] - Claude 4 Sonnet (latest, most capable)")
    display.print("  • [yellow]claude-3.5-sonnet[/yellow] - Claude 3.5 Sonnet (excellent coding)")
    display.print("  • [yellow]claude-3.5-haiku[/yellow] - Claude 3.5 Haiku (fast, affordable)")
    display.print("  • [yellow]claude-3-opus[/yellow] - Claude 3 Opus (most creative)")
    display.print("\n  [dim]Requires: Anthropic API key[/dim]")
    
    display.print("\n[bold cyan]Groq Provider[/bold cyan]")
    display.print("  • [yellow]openai/gpt-oss-120b[/yellow] - GPT OSS 120B (via Groq router)")
    display.print("  • [yellow]openai/gpt-oss-20b[/yellow] - GPT OSS 20B (via Groq router)")
    display.print("  • [yellow]llama3-70b-8192[/yellow] - Llama 3 70B")
    display.print("  • [yellow]llama3-8b-8192[/yellow] - Llama 3 8B")
    display.print("\n  [dim]Requires: Groq API key[/dim]")
    
    display.print("\n[bold cyan]DeepSeek Provider[/bold cyan]")
    display.print("  • [yellow]deepseek-v3.2[/yellow] - DeepSeek-V3.2 / Chat")
    display.print("  • [yellow]deepseek-r1[/yellow] - DeepSeek-R1 / Reasoner")
    display.print("\n  [dim]Requires: DeepSeek API key[/dim]")
    
    display.print("\n[bold cyan]LM Studio Provider[/bold cyan]")
    display.print("  • [yellow]lmstudio[/yellow] - Use any local model")
    display.print("\n  [dim]Requires: LM Studio running locally[/dim]")

    display.print("\n[bold cyan]Ollama Provider[/bold cyan]")
    display.print("  • [yellow]ollama[/yellow] - Use any local model hosted via Ollama")
    display.print("\n  [dim]Requires: Ollama running locally[/dim]")
    
    config = config_manager.load()
    display.print(f"\n[bold]Current default:[/bold] [yellow]{config.default_model}[/yellow]")
    display.print()


@cli.command()
@click.argument("model_name")
def set_model(model_name):
    """Set default model for new sessions."""
    from .llm.openai import OpenAIProvider
    from .llm.anthropic import MODEL_ALIASES
    from .llm.groq import GroqProvider
    from .llm.deepseek import DeepSeekProvider
    
    valid_models = list(OpenAIProvider.SUPPORTED_MODELS.keys()) + list(MODEL_ALIASES.keys()) + list(GroqProvider.SUPPORTED_MODELS.keys()) + list(DeepSeekProvider.SUPPORTED_MODELS.keys()) + ["lmstudio", "ollama"]
    
    if model_name not in valid_models:
        display.print_error(f"Invalid model: {model_name}")
        display.print_info(f"Valid models: {', '.join(valid_models)}")
        display.print_info("Run 'coderAI models' to see all available models")
        return
    
    config_manager.set("default_model", model_name)
    display.print_success(f"Default model set to: {model_name}")


@cli.command()
def cost():
    """Show API cost tracking and pricing info."""
    from .cost import MODEL_PRICING, CostTracker
    
    display.print_header("API Cost Tracking")
    display.print_info("Cost tracking is available during active chat sessions.")
    display.print_info("Use '/tokens' in chat to see current session costs.")
    
    config = config_manager.load()
    if config.budget_limit > 0:
        display.print(f"\n[bold blue]Active Budget Limit:[/bold blue] {CostTracker.format_cost(config.budget_limit)}")
    
    display.print("\n[dim]Per-model pricing (per 1M tokens):[/dim]")
    for model, pricing in MODEL_PRICING.items():
        if pricing["input"] == 0 and pricing["output"] == 0:
            display.print(f"  [yellow]{model}[/yellow]: Free (local)")
        else:
            display.print(f"  [yellow]{model}[/yellow]: "
                          f"{CostTracker.format_cost(pricing['input'])} input / "
                          f"{CostTracker.format_cost(pricing['output'])} output")
    display.print()


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
    display.print(f"  Log level: {config.log_level}")
    
    # Check OpenAI
    display.print("\n[bold cyan]OpenAI Provider:[/bold cyan]")
    if config.openai_api_key:
        display.print("  ✓ API key configured")
    else:
        display.print("  ✗ API key not configured")
        display.print("    [dim]Run 'coderAI setup' or 'coderAI config set openai_api_key YOUR_KEY'[/dim]")
    
    # Check Anthropic
    display.print("\n[bold cyan]Anthropic Provider:[/bold cyan]")
    if config.anthropic_api_key:
        display.print("  ✓ API key configured")
    else:
        display.print("  ✗ API key not configured")
        display.print("    [dim]Run 'coderAI setup' or 'coderAI config set anthropic_api_key YOUR_KEY'[/dim]")
        
    # Check Groq
    display.print("\n[bold cyan]Groq Provider:[/bold cyan]")
    if config.groq_api_key:
        display.print("  ✓ API key configured")
    else:
        display.print("  ✗ API key not configured")
        display.print("    [dim]Run 'coderAI setup' or 'coderAI config set groq_api_key YOUR_KEY'[/dim]")

    # Check DeepSeek
    display.print("\n[bold cyan]DeepSeek Provider:[/bold cyan]")
    if config.deepseek_api_key:
        display.print("  ✓ API key configured")
    else:
        display.print("  ✗ API key not configured")
        display.print("    [dim]Run 'coderAI setup' or 'coderAI config set deepseek_api_key YOUR_KEY'[/dim]")
    
    # Check LM Studio
    display.print("\n[bold cyan]LM Studio Provider:[/bold cyan]")
    display.print(f"  Endpoint: {config.lmstudio_endpoint}")
    display.print(f"  Model: {config.lmstudio_model}")
    
    # Check Ollama
    display.print("\n[bold cyan]Ollama Provider:[/bold cyan]")
    display.print(f"  Endpoint: {config.ollama_endpoint}")
    display.print(f"  Model: {config.ollama_model}")
    
    # Check history
    from .history import history_manager
    sessions = history_manager.list_sessions()
    display.print("\n[bold cyan]History:[/bold cyan]")
    display.print(f"  History dir: {history_manager.history_dir}")
    display.print(f"  Total sessions: {len(sessions)}")
    
    display.print()


@cli.group()
def tasks():
    """Manage project tasks and TODOs."""
    pass


@tasks.command("list")
def tasks_list():
    """List all tasks."""
    from .tools.tasks import ManageTasksTool
    import asyncio
    
    tool = ManageTasksTool()
    result = asyncio.run(tool.execute("list"))
    if not result.get("success"):
        from .ui.display import display
        display.print_error(result.get("error", "Unknown error"))
        return
        
    from .ui.display import display
    display.print_header(result.get("summary", "Tasks"))
    
    for status, color in [("pending", "yellow"), ("completed", "green")]:
        task_list = result.get(status, [])
        if task_list:
            display.print(f"\n[bold {color}]{status.title()} Tasks:[/bold {color}]")
            for t in task_list:
                desc = f" - {t['description']}" if t.get("description") else ""
                display.print(f"  [{t['id']}] {t['title']}{desc}")


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
