"""Main CLI group and top-level commands (chat, info, doctor, status, models, cost)."""

import logging
import sys

import click
from dotenv import load_dotenv

load_dotenv()

from coderAI import __version__
from coderAI.system.config import config_manager
from coderAI.system.history import history_manager
from coderAI.system.logging_setup import setup_logging

from .config_cmd import config
from .history_cmd import history
from .setup import setup
from .index_cmd import index_cmd, search_cmd
from .mcp_cmd import mcp as mcp_cmd
from .run_cmd import run as run_cmd
from .tasks_cmd import tasks
from .utils import missing_api_key_message, valid_models

logger = logging.getLogger(__name__)

_REASONING_CHOICES = ("high", "medium", "low", "none")


@click.group(invoke_without_command=True)
@click.option("--version", "-v", is_flag=True, help="Show version")
@click.option("--verbose", is_flag=True, help="Enable verbose/debug logging")
@click.option("--model", "-m", help="Model to use")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option(
    "--continue",
    "resume_latest",
    is_flag=True,
    help="Resume the most recently updated session",
)
@click.pass_context
def cli(
    ctx: click.Context,
    version: bool,
    verbose: bool,
    model: str | None,
    resume: str | None,
    resume_latest: bool,
) -> None:
    """CoderAI - Intelligent Coding Agent CLI Tool.

    Run 'coderAI chat' for interactive mode.
    """
    if version:
        click.echo(f"CoderAI version {__version__}")
        sys.exit(0)

    if not logging.getLogger().handlers:
        setup_logging(logging.DEBUG if verbose else None)

    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    if ctx.invoked_subcommand is None:
        ctx.invoke(
            chat,
            auto_approve=False,
            persona=None,
            model=model,
            resume=resume,
            resume_latest=resume_latest,
        )


@cli.command()
@click.option("--model", "-m", help="Model to use")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option(
    "--continue-session",
    "resume_latest",
    is_flag=True,
    help="Resume the most recently updated session",
)
@click.option(
    "--continue",
    "resume_latest",
    is_flag=True,
    help="Resume the most recently updated session (alias for --continue-session)",
)
@click.option("--auto-approve", "--yolo", is_flag=True, help="Skip tool confirmation prompts")
@click.option(
    "--persona",
    "-p",
    default=None,
    help="Persona to load at startup (filename stem in .coderAI/agents/, e.g. 'code-reviewer'). "
    "Personas can also be switched mid-session with /persona <name>.",
)
def chat(
    model: str | None,
    resume: str | None,
    resume_latest: bool,
    auto_approve: bool,
    persona: str | None,
) -> None:
    """Start an interactive chat session (Textual TUI)."""
    from coderAI.tui import run_chat_app
    from coderAI.ui.display import display

    from .bootstrap import BootstrapError, resolve_resume_id

    key_error = missing_api_key_message()
    if key_error:
        display.print_error(key_error)
        sys.exit(1)

    # Resolve --resume/--continue up front so a bad combination is reported
    # cleanly before the Textual UI launches (shared with the headless path).
    try:
        resume = resolve_resume_id(resume, resume_latest)
    except BootstrapError as e:
        click.echo(e.message, err=True)
        sys.exit(e.exit_code)
    if resume_latest and resume:
        click.echo(f"Resuming session {resume}")

    if auto_approve:
        display.print_warning(
            "Auto-approve enabled — tool calls will run without confirmation. "
            "This includes run_command, delete_file, and git_push. "
            "Press Ctrl+C to abort."
        )

    try:
        # ``resume`` is already resolved above (``--continue`` → concrete id), so
        # continue_=False prevents the session bootstrap from resolving it again.
        run_chat_app(
            model=model,
            resume=resume,
            continue_=False,
            auto_approve=auto_approve,
            persona=persona,
        )
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option("--model", "-m", help="Model to use")
def info(model: str | None) -> None:
    """Show information about the agent and model."""
    from coderAI.core.agent import Agent
    from coderAI.ui.display import display

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
        tools_info.append(
            {
                "Tool": tool.name,
                "Description": tool.description[:60] + "..."
                if len(tool.description) > 60
                else tool.description,
            }
        )
    display.print_table(tools_info)


@cli.command()
def models() -> None:
    """List available models and providers."""
    from coderAI.llm.factory import get_models_by_provider
    from coderAI.ui.display import display

    display.print_header("Available Models and Providers")

    def _print_group(title: str, names: list[str], requires: str) -> None:
        display.print(f"\n[bold cyan]{title}[/bold cyan]")
        for name in names:
            display.print(f"  • [yellow]{name}[/yellow]")
        display.print(f"\n  [dim]Requires: {requires}[/dim]")

    for title, names, requires in get_models_by_provider():
        _print_group(title, names, requires)

    config = config_manager.load()
    display.print(f"\n[bold]Current default:[/bold] [yellow]{config.default_model}[/yellow]")
    display.print()


@cli.command()
@click.argument("model_name")
def set_model(model_name: str) -> None:
    """Set default model for new sessions."""
    from coderAI.ui.display import display

    valid = valid_models()
    if model_name not in valid:
        display.print_error(f"Invalid model: {model_name}")
        display.print_info("Run 'coderAI models' to see all available models")
        sys.exit(2)

    config_manager.set("default_model", model_name)
    display.print_success(f"Default model set to: {model_name}")


@cli.command()
def cost() -> None:
    """Show API cost tracking and pricing info."""
    from coderAI.system.cost import MODEL_PRICING, CostTracker
    from coderAI.ui.display import display

    display.print_header("API Cost Tracking")
    display.print_info("Cost tracking is available during active chat sessions.")
    display.print_info("Use '/tokens' in chat to see current session costs.")

    config = config_manager.load()
    if config.budget_limit > 0:
        display.print(
            f"\n[bold blue]Active Budget Limit:[/bold blue] {CostTracker.format_cost(config.budget_limit)}"
        )

    display.print("\n[dim]Per-model pricing (per 1M tokens):[/dim]")
    for model, pricing in MODEL_PRICING.items():
        if pricing["input"] == 0 and pricing["output"] == 0:
            display.print(f"  [yellow]{model}[/yellow]: Free (local)")
        else:
            display.print(
                f"  [yellow]{model}[/yellow]: "
                f"{CostTracker.format_cost(pricing['input'])} input / "
                f"{CostTracker.format_cost(pricing['output'])} output"
            )
    display.print()


@cli.command()
def status() -> None:
    """Show system status and diagnostics."""
    from coderAI.ui.display import display

    display.print_header("CoderAI System Status")

    config = config_manager.load()

    display.print("\n[bold cyan]Configuration[/bold cyan]")
    display.print(f"  config dir     {config_manager.config_dir}")
    display.print(f"  default model  [yellow]{config.default_model}[/yellow]")
    display.print(f"  streaming      {'enabled' if config.streaming else 'disabled'}")
    display.print(f"  save history   {'enabled' if config.save_history else 'disabled'}")
    display.print(f"  log level      {config.log_level.lower()}")

    display.print("\n[bold cyan]Cloud providers[/bold cyan]")

    def _key_row(label: str, configured: bool, hint_key: str) -> None:
        if configured:
            display.print(f"  {label:<12}  ✓ key configured")
        else:
            display.print(f"  {label:<12}  ✗ key missing")
            display.print(f"                [dim]coderAI config set {hint_key} <YOUR_KEY>[/dim]")

    _key_row("OpenAI", bool(config.openai_api_key), "openai_api_key")
    _key_row("Anthropic", bool(config.anthropic_api_key), "anthropic_api_key")
    _key_row("Groq", bool(config.groq_api_key), "groq_api_key")
    _key_row("DeepSeek", bool(config.deepseek_api_key), "deepseek_api_key")
    _key_row("Gemini", bool(config.gemini_api_key), "gemini_api_key")

    display.print("\n[bold cyan]Local providers[/bold cyan]")
    display.print(f"  LM Studio     endpoint {config.lmstudio_endpoint}")
    display.print(f"                model    {config.lmstudio_model}")
    display.print(f"  Ollama        endpoint {config.ollama_endpoint}")
    display.print(f"                model    {config.ollama_model}")

    sessions = history_manager.list_sessions()
    display.print("\n[bold cyan]History[/bold cyan]")
    display.print(f"  history dir     {history_manager.history_dir}")
    display.print(f"  saved sessions  {len(sessions)}")

    display.print()


@cli.command()
def doctor() -> None:
    """Diagnose a CoderAI install — config, keys, cache, binary."""
    import os
    import platform
    import tempfile
    import importlib.util

    from coderAI.ui.display import display

    display.print_header("CoderAI Doctor")

    ok_count = 0
    warn_count = 0
    fail_count = 0

    def check_ok(label: str, detail: str = "") -> None:
        nonlocal ok_count
        ok_count += 1
        suffix = f"  [dim]{detail}[/dim]" if detail else ""
        display.print(f"  [green]✓[/green] {label}{suffix}")

    def check_warn(label: str, detail: str = "") -> None:
        nonlocal warn_count
        warn_count += 1
        suffix = f"  [dim]{detail}[/dim]" if detail else ""
        display.print(f"  [yellow]⚠[/yellow] {label}{suffix}")

    def check_fail(label: str, detail: str = "") -> None:
        nonlocal fail_count
        fail_count += 1
        suffix = f"  [dim]{detail}[/dim]" if detail else ""
        display.print(f"  [red]✗[/red] {label}{suffix}")

    # 1. Python
    display.print("\n[bold cyan]Runtime[/bold cyan]")
    check_ok(
        f"Python {platform.python_version()}", f"{platform.system().lower()}-{platform.machine()}"
    )
    check_ok(f"CoderAI {__version__}")

    # 2. Config directory
    display.print("\n[bold cyan]Config[/bold cyan]")
    cfg_dir = config_manager.config_dir
    if not cfg_dir.exists():
        check_fail(f"{cfg_dir} missing", "run `coderAI setup` to create it")
    elif not os.access(cfg_dir, os.W_OK):
        check_fail(f"{cfg_dir} not writable")
    else:
        try:
            with tempfile.NamedTemporaryFile(dir=cfg_dir, prefix=".doctor-", delete=True):
                pass
            check_ok(f"{cfg_dir} writable")
        except OSError as e:
            check_fail(f"{cfg_dir} write test failed", str(e))

    if os.name != "nt":
        for label, path, want in (
            ("config dir", config_manager.config_dir, 0o700),
            ("config file", config_manager.config_file, 0o600),
        ):
            if not path.exists():
                continue
            mode = path.stat().st_mode & 0o777
            if mode & ~want:
                check_warn(
                    f"{label} permissions too open ({oct(mode)})",
                    f"run `chmod {oct(want)[2:]} {path}` — it can contain API keys",
                )
            else:
                check_ok(f"{label} permissions {oct(mode)}")

    cfg = config_manager.load()
    if cfg.default_model:
        check_ok(f"default model: {cfg.default_model}")
    else:
        check_warn("default model not set", "run `coderAI setup`")

    # 3. Providers
    display.print("\n[bold cyan]Providers[/bold cyan]")
    keys = [
        ("OpenAI", cfg.openai_api_key),
        ("Anthropic", cfg.anthropic_api_key),
        ("Groq", cfg.groq_api_key),
        ("DeepSeek", cfg.deepseek_api_key),
        ("Gemini", cfg.gemini_api_key),
    ]
    any_cloud = any(v for _, v in keys)
    for name, val in keys:
        if val:
            masked = f"{val[:4]}…{val[-4:]}" if len(val) > 8 else "set"
            check_ok(f"{name}: {masked}")
        else:
            check_warn(f"{name}: not configured")
    if not any_cloud and (cfg.default_model or "").lower() not in ("lmstudio", "ollama"):
        check_fail(
            "No cloud key and default is not lmstudio/ollama",
            "chat won't start until one is set",
        )

    # 4. Textual TUI
    display.print("\n[bold cyan]Interactive UI[/bold cyan]")
    if importlib.util.find_spec("textual") is not None:
        check_ok("textual installed (coderAI chat)")
    else:
        check_fail("textual not installed", "pip install 'coderAI' or textual>=0.80")

    # 5. History
    display.print("\n[bold cyan]History[/bold cyan]")
    try:
        n = len(history_manager.list_sessions())
        check_ok(f"{history_manager.history_dir} ({n} sessions)")
    except Exception as e:
        check_warn(f"{history_manager.history_dir}", str(e))

    # Summary
    display.print()
    summary = f"{ok_count} ok · {warn_count} warn · {fail_count} fail"
    if fail_count:
        display.print_error(summary)
        sys.exit(1)
    elif warn_count:
        display.print_warning(summary)
    else:
        display.print_success(summary)
    display.print()


# Register subcommand groups
cli.add_command(config)
cli.add_command(history)
cli.add_command(tasks)
cli.add_command(setup)
cli.add_command(index_cmd)
cli.add_command(search_cmd)
cli.add_command(run_cmd)
cli.add_command(mcp_cmd)


def main() -> None:
    """Main entry point."""
    cli()
