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

logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.option(
    "--model",
    "-m",
    help=(
        "Model or alias (claude-sonnet-4-6, opus, haiku, gpt-5.4-mini, …). "
        "Run `coderAI models` for the full list."
    ),
)
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option(
    "--continue", "continue_", is_flag=True,
    help="Resume the most recently updated session"
)
@click.option("--version", "-v", is_flag=True, help="Show version")
@click.option("--verbose", is_flag=True, help="Enable verbose/debug logging")
@click.pass_context
def cli(ctx, model, resume, continue_, version, verbose):
    """CoderAI - Intelligent Coding Agent CLI Tool.

    Run 'coderAI chat' for interactive mode.
    """
    if version:
        click.echo(f"CoderAI version {__version__}")
        sys.exit(0)

    # Set up logging once at the CLI entry point. Level comes from --verbose
    # when set, otherwise from the loaded config (default: WARNING). We only
    # configure if no handler is installed yet so embedders can pre-configure.
    if not logging.getLogger().handlers:
        if verbose:
            level = logging.DEBUG
        else:
            cfg_level = getattr(config_manager.load(), "log_level", "WARNING")
            level = getattr(logging, cfg_level, logging.WARNING)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    # Store options in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose

    # If no subcommand, default to chat
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat, model=model, resume=resume, continue_=continue_)


@cli.command()
@click.option("--model", "-m", help="Model to use")
@click.option("--resume", "-r", help="Resume a previous session by ID")
@click.option(
    "--continue", "continue_", is_flag=True,
    help="Resume the most recently updated session"
)
@click.option("--auto-approve", "--yolo", is_flag=True, help="Skip tool confirmation prompts")
@click.option("--python", default=None, help="Python interpreter for the agent (defaults to current)")
def chat(model, resume, continue_, auto_approve, python):
    """Start an interactive chat session in the Ink UI.

    On first run, downloads the prebuilt UI binary for the current platform
    from GitHub Releases (verified by SHA256) and caches it under
    ``~/.coderAI/bin``. Set ``$CODERAI_UI_BINARY`` to bypass the download.
    """
    import os
    import subprocess

    from .binary_manager import (
        BinaryUnavailableError,
        UnsupportedPlatformError,
        ensure_binary,
    )

    # Preflight: make sure the user has at least one provider configured
    # before we spawn the UI. Otherwise the chat opens to a friendly
    # status bar and then fails silently on the first send.
    cfg = config_manager.load()
    has_cloud_key = any(
        [
            getattr(cfg, "openai_api_key", None),
            getattr(cfg, "anthropic_api_key", None),
            getattr(cfg, "groq_api_key", None),
            getattr(cfg, "deepseek_api_key", None),
        ]
    )
    # Local providers (lmstudio/ollama) don't need an API key — detect
    # by either default_model hint or a configured endpoint being set to
    # something other than the library default.
    local_default = (cfg.default_model or "").lower() in ("lmstudio", "ollama")
    if not has_cloud_key and not local_default:
        display.print_error(
            "No API key configured. Run `coderAI setup` to add one, or set a "
            "provider env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, "
            "DEEPSEEK_API_KEY). For local models, run `coderAI config set "
            "default_model lmstudio` (or ollama)."
        )
        sys.exit(1)

    try:
        binary = ensure_binary(__version__)
    except (BinaryUnavailableError, UnsupportedPlatformError) as e:
        display.print_error(str(e))
        sys.exit(1)

    if continue_:
        if resume:
            click.echo("Pass either --resume or --continue, not both.", err=True)
            sys.exit(2)
        sid = history_manager.get_latest_session_id()
        if not sid:
            click.echo("No previous sessions found.", err=True)
            sys.exit(1)
        resume = sid
        click.echo(f"Resuming session {sid}")

    env = os.environ.copy()
    if model:
        env["CODERAI_MODEL"] = model
    if resume:
        env["CODERAI_RESUME"] = resume
    if auto_approve:
        env["CODERAI_AUTO_APPROVE"] = "1"
        display.print_warning(
            "Auto-approve enabled — tool calls will run without confirmation. "
            "This includes run_command, delete_file, and git_push. "
            "Press Ctrl+C to abort."
        )
    env["CODERAI_PYTHON"] = python or sys.executable

    try:
        result = subprocess.run([str(binary)], env=env, check=False)
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError:
        display.print_error(
            f"UI binary at {binary} is not executable. "
            "Try `make ui-compile` to rebuild, or delete the cache at "
            "`~/.coderAI/bin/` to force a re-download."
        )
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
        elif key in ["max_tokens", "context_window", "max_iterations", "max_tool_output"]:
            value = int(value)
        elif key in ["streaming", "save_history", "web_tools_in_main"]:
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


_REASONING_CHOICES = ("high", "medium", "low", "none")


def _valid_models() -> set[str]:
    """Return the set of valid default-model values accepted by setup()."""
    from .llm.anthropic import MODEL_ALIASES as _ANTH
    from .llm.deepseek import DeepSeekProvider as _DS
    from .llm.groq import GroqProvider as _GR
    from .llm.openai import OpenAIProvider as _OAI

    return (
        set(_OAI.SUPPORTED_MODELS.keys())
        | set(_ANTH.keys())
        | set(_GR.SUPPORTED_MODELS.keys())
        | set(_DS.SUPPORTED_MODELS.keys())
        | {"lmstudio", "ollama"}
    )


def _valid_endpoint(url: str) -> bool:
    """Loose URL check: must start with http:// or https:// and have a host."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


@cli.command()
def setup():
    """Interactive setup wizard."""
    display.print_header("CoderAI Setup Wizard")
    display.print()

    captured_any_key = False

    # OpenAI API Key
    display.print("[bold]1. OpenAI API Key[/bold]")
    display.print("   Required for using GPT models")
    api_key = click.prompt(
        "   Enter your OpenAI API key (or press Enter to skip)",
        default="",
        show_default=False,
    )
    if api_key:
        config_manager.set("openai_api_key", api_key)
        display.print_success("   OpenAI API key saved")
        captured_any_key = True
    display.print()

    # Anthropic API Key
    display.print("[bold]2. Anthropic API Key[/bold]")
    display.print("   Required for using Claude models")
    anthropic_key = click.prompt(
        "   Enter your Anthropic API key (or press Enter to skip)",
        default="",
        show_default=False,
    )
    if anthropic_key:
        config_manager.set("anthropic_api_key", anthropic_key)
        display.print_success("   Anthropic API key saved")
        captured_any_key = True
    display.print()

    # Groq API Key
    display.print("[bold]3. Groq API Key[/bold]")
    display.print("   Required for using Groq models (including openai/gpt-oss-120b and openai/gpt-oss-20b)")
    groq_key = click.prompt(
        "   Enter your Groq API key (or press Enter to skip)",
        default="",
        show_default=False,
    )
    if groq_key:
        config_manager.set("groq_api_key", groq_key)
        display.print_success("   Groq API key saved")
        captured_any_key = True
    display.print()

    # DeepSeek API Key
    display.print("[bold]4. DeepSeek API Key[/bold]")
    display.print("   Required for using DeepSeek models")
    deepseek_key = click.prompt(
        "   Enter your DeepSeek API key (or press Enter to skip)",
        default="",
        show_default=False,
    )
    if deepseek_key:
        config_manager.set("deepseek_api_key", deepseek_key)
        display.print_success("   DeepSeek API key saved")
        captured_any_key = True
    display.print()

    # Default Model — validated against the provider registries.
    valid = _valid_models()
    display.print("[bold]5. Default Model[/bold]")
    display.print("   Run `coderAI models` after setup for the full list.")
    display.print(
        "   Common: claude-sonnet-4-6, opus, haiku, gpt-5.4-mini, "
        "gpt-5.4, deepseek-v4-flash, deepseek-v4-pro, lmstudio, ollama"
    )
    while True:
        model = click.prompt("   Enter default model", default="gpt-5.4-mini").strip()
        if model in valid:
            config_manager.set("default_model", model)
            display.print_success(f"   Default model set to {model}")
            break
        display.print_error(
            f"   Unknown model: {model}. Run `coderAI models` for the full list."
        )
    display.print()

    # Reasoning Effort — whitelisted.
    display.print("[bold]6. Reasoning Effort[/bold]")
    display.print(
        "   Thinking budget for reasoning-capable models (o1, o3-mini, "
        "gpt-5.4, claude-sonnet-4-6, …)."
    )
    effort = click.prompt(
        "   Enter reasoning effort",
        default="medium",
        type=click.Choice(_REASONING_CHOICES, case_sensitive=False),
        show_choices=True,
    ).lower()
    config_manager.set("reasoning_effort", effort)
    display.print_success(f"   Reasoning effort set to {effort}")
    display.print()

    # LM Studio (optional)
    display.print("[bold]7. LM Studio Configuration (Optional)[/bold]")
    display.print("   For using local models with LM Studio")
    use_lmstudio = click.confirm("   Configure LM Studio?", default=False)
    if use_lmstudio:
        while True:
            endpoint = click.prompt(
                "   LM Studio server URL", default="http://localhost:1234/v1"
            ).strip()
            if _valid_endpoint(endpoint):
                config_manager.set("lmstudio_endpoint", endpoint)
                break
            display.print_error(
                "   Endpoint must be a full http(s)://host:port/v1 URL."
            )
        model_name = click.prompt(
            "   LM Studio model name (optional)",
            default="local-model",
            show_default=True,
        )
        config_manager.set("lmstudio_model", model_name)
        display.print_success("   LM Studio configuration saved")
    display.print()

    # Ollama (optional)
    display.print("[bold]8. Ollama Configuration (Optional)[/bold]")
    display.print("   For using local models with Ollama")
    use_ollama = click.confirm("   Configure Ollama?", default=False)
    if use_ollama:
        while True:
            endpoint = click.prompt(
                "   Ollama server URL", default="http://localhost:11434/v1"
            ).strip()
            if _valid_endpoint(endpoint):
                config_manager.set("ollama_endpoint", endpoint)
                break
            display.print_error(
                "   Endpoint must be a full http(s)://host:port/v1 URL."
            )
        model_name = click.prompt(
            "   Ollama model name", default="llama3", show_default=True
        )
        config_manager.set("ollama_model", model_name)
        display.print_success("   Ollama configuration saved")
    display.print()

    if not captured_any_key and not (use_lmstudio or use_ollama):
        display.print_warning(
            "No API keys entered and no local provider configured. "
            "`coderAI chat` will refuse to start until one is set — re-run "
            "`coderAI setup` or set an env var (ANTHROPIC_API_KEY, etc.)."
        )
    else:
        display.print_success("Setup complete! Run 'coderAI chat' to start.")


@cli.command()
def models():
    """List available models and providers."""
    from .llm.anthropic import MODEL_ALIASES as ANTHROPIC_ALIASES
    from .llm.deepseek import DeepSeekProvider
    from .llm.groq import GroqProvider
    from .llm.openai import OpenAIProvider

    display.print_header("Available Models and Providers")

    def _print_group(title: str, names, requires: str) -> None:
        display.print(f"\n[bold cyan]{title}[/bold cyan]")
        for name in names:
            display.print(f"  • [yellow]{name}[/yellow]")
        display.print(f"\n  [dim]Requires: {requires}[/dim]")

    _print_group(
        "OpenAI Provider",
        OpenAIProvider.SUPPORTED_MODELS.keys(),
        "OpenAI API key",
    )
    _print_group(
        "Anthropic Provider",
        ANTHROPIC_ALIASES.keys(),
        "Anthropic API key",
    )
    _print_group(
        "Groq Provider",
        GroqProvider.SUPPORTED_MODELS.keys(),
        "Groq API key",
    )
    _print_group(
        "DeepSeek Provider",
        DeepSeekProvider.SUPPORTED_MODELS.keys(),
        "DeepSeek API key",
    )
    _print_group("LM Studio Provider", ["lmstudio"], "LM Studio running locally")
    _print_group("Ollama Provider", ["ollama"], "Ollama running locally")

    config = config_manager.load()
    display.print(f"\n[bold]Current default:[/bold] [yellow]{config.default_model}[/yellow]")
    display.print()


@cli.command()
@click.argument("model_name")
def set_model(model_name):
    """Set default model for new sessions."""
    valid = _valid_models()
    if model_name not in valid:
        display.print_error(f"Invalid model: {model_name}")
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


def _on_off(flag: bool) -> str:
    return "enabled" if flag else "disabled"


def _key_row(label: str, configured: bool, hint_key: str) -> None:
    if configured:
        display.print(f"  {label:<12}  ✓ key configured")
    else:
        display.print(f"  {label:<12}  ✗ key missing")
        display.print(
            f"                [dim]coderAI config set {hint_key} <YOUR_KEY>[/dim]"
        )


@cli.command()
def status():
    """Show system status and diagnostics."""
    display.print_header("CoderAI System Status")

    config = config_manager.load()

    display.print("\n[bold cyan]Configuration[/bold cyan]")
    display.print(f"  config dir     {config_manager.config_dir}")
    display.print(f"  default model  [yellow]{config.default_model}[/yellow]")
    display.print(f"  streaming      {_on_off(config.streaming)}")
    display.print(f"  save history   {_on_off(config.save_history)}")
    display.print(f"  log level      {config.log_level.lower()}")

    display.print("\n[bold cyan]Cloud providers[/bold cyan]")
    _key_row("OpenAI", bool(config.openai_api_key), "openai_api_key")
    _key_row("Anthropic", bool(config.anthropic_api_key), "anthropic_api_key")
    _key_row("Groq", bool(config.groq_api_key), "groq_api_key")
    _key_row("DeepSeek", bool(config.deepseek_api_key), "deepseek_api_key")

    display.print("\n[bold cyan]Local providers[/bold cyan]")
    display.print(f"  LM Studio     endpoint {config.lmstudio_endpoint}")
    display.print(f"                model    {config.lmstudio_model}")
    display.print(f"  Ollama        endpoint {config.ollama_endpoint}")
    display.print(f"                model    {config.ollama_model}")

    from .history import history_manager

    sessions = history_manager.list_sessions()
    display.print("\n[bold cyan]History[/bold cyan]")
    display.print(f"  history dir     {history_manager.history_dir}")
    display.print(f"  saved sessions  {len(sessions)}")

    display.print()


@cli.command()
def doctor():
    """Diagnose a CoderAI install — config, keys, cache, binary."""
    import os
    import platform
    import tempfile
    from pathlib import Path

    from .binary_manager import (
        BinaryUnavailableError,
        UnsupportedPlatformError,
        detect_platform,
        local_dev_binary,
    )

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
        f"Python {platform.python_version()}",
        f"{platform.system().lower()}-{platform.machine()}",
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
        # Round-trip write test
        try:
            with tempfile.NamedTemporaryFile(
                dir=cfg_dir, prefix=".doctor-", delete=True
            ):
                pass
            check_ok(f"{cfg_dir} writable")
        except OSError as e:
            check_fail(f"{cfg_dir} write test failed", str(e))

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

    # 4. UI binary
    display.print("\n[bold cyan]Ink UI binary[/bold cyan]")
    override = os.environ.get("CODERAI_UI_BINARY")
    dev = local_dev_binary()
    if override:
        p = Path(override).expanduser()
        (check_ok if p.is_file() else check_fail)(
            f"$CODERAI_UI_BINARY → {p}",
            "exists" if p.is_file() else "not found",
        )
    elif dev is not None:
        check_ok(f"dev binary: {dev}")
    else:
        try:
            plat = detect_platform()
            check_ok(f"platform resolved: {plat}")
        except (UnsupportedPlatformError, BinaryUnavailableError) as e:
            check_fail("platform not supported", str(e))

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


@cli.group()
def tasks():
    """Manage project tasks and TODOs."""
    pass


@cli.command("index")
@click.option("--force", is_flag=True, help="Re-index all files, ignoring the manifest cache")
@click.option(
    "--paths", "-p", multiple=True,
    help="Specific files or directories to index (can be repeated). If omitted, indexes the whole project."
)
def index_cmd(force, paths):
    """Build or update the semantic code search index.

    Walks the project, splits source files into semantic chunks, generates
    embeddings via the configured provider, and stores them in a local
    vector database under .coderAI/index/.

    Requires an OpenAI API key (for embeddings). Subsequent runs only
    re-index changed files unless --force is used.
    """
    import asyncio

    from .config import config_manager
    from .embeddings.factory import create_embedding_provider
    from .code_indexer import CodeIndexer
    from .ui.display import display

    config = config_manager.load()
    provider = create_embedding_provider(config)
    if provider is None:
        display.print_error(
            "No embedding provider available. Set openai_api_key via "
            "`coderAI config set openai_api_key <key>` or OPENAI_API_KEY env var."
        )
        sys.exit(1)

    project_root = str(Path(config.project_root).resolve())
    indexer = CodeIndexer(project_root, provider)

    display.print_info(f"Project root: {project_root}")
    display.print_info("Indexing project (this may take a while on first run)...")

    try:
        result = asyncio.run(
            indexer.index(
                skip_if_unchanged=not force,
                paths=list(paths) if paths else None,
            )
        )
    except Exception as e:
        display.print_error(f"Indexing failed: {e}")
        sys.exit(1)

    stats = indexer.stats()
    display.print_success(
        f"Index updated: {result['added']} added, {result['updated']} updated, "
        f"{result['removed']} removed, {result['unchanged']} unchanged. "
        f"Total: {stats['chunks']} chunks from {stats['indexed_files']} files."
    )


@cli.command("search")
@click.argument("query")
@click.option("--top-k", "-n", default=10, help="Number of results (default: 10)")
@click.option("--file-filter", "-f", default=None, help="Glob to filter results, e.g. '*.py'")
def search_cmd(query, top_k, file_filter):
    """Search the codebase with a natural-language query.

    Requires a pre-built index. Run `coderAI index` first.

    Examples:
      coderAI search "where is authentication middleware?"
      coderAI search "rate limiting logic" -f "*.py"
    """
    import asyncio

    from .config import config_manager
    from .embeddings.factory import create_embedding_provider
    from .code_indexer import CodeIndexer
    from .ui.display import display

    config = config_manager.load()
    provider = create_embedding_provider(config)
    if provider is None:
        display.print_error(
            "No embedding provider available. Set openai_api_key."
        )
        sys.exit(1)

    project_root = str(Path(config.project_root).resolve())
    indexer = CodeIndexer(project_root, provider)

    try:
        results = asyncio.run(
            indexer.search(query=query, top_k=top_k, file_filter=file_filter)
        )
    except Exception as e:
        display.print_error(f"Search failed: {e}")
        sys.exit(1)

    if not results:
        display.print_warning("No results found. Is the index built? Run `coderAI index`.")
        return

    display.print_header(f"Semantic search results for: \"{query}\"")
    for i, r in enumerate(results, 1):
        display.print(
            f"\n[bold]{i}.[/bold] [cyan]{r['file_path']}[/cyan] "
            f"lines {r['start_line']}-{r['end_line']} "
            f"[dim]({r['language']}, score: {r['score']:.3f})[/dim]"
        )
        # Show first 3 lines of the chunk
        snippet = r["text"][:300]
        if len(r["text"]) > 300:
            snippet += "..."
        for line in snippet.split("\n")[:5]:
            display.print(f"    {line}")


@tasks.command("list")
def tasks_list():
    """List all tasks."""
    from .tools.tasks import ManageTasksTool
    
    tool = ManageTasksTool()
    result = asyncio.run(tool.execute("list"))
    if not result.get("success"):
        display.print_error(result.get("error", "Unknown error"))
        return

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
