"""Interactive setup wizard and configuration subsystem for CoderAI.

Provides both interactive TUI wizard (/setup, coderai setup) and non-interactive
scriptable CLI options for configuring API keys, providers, custom endpoints, and models.
"""

from __future__ import annotations

import getpass
import sys
from typing import Any, Literal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from coderai.cli.interactive_menu import select_with_arrows
from coderai.core.common.model_capabilities import (
    CURATED_MODELS,
    get_model_badges,
)
from coderai.core.openai_client import clear_client_pool, probe_provider_connectivity
from coderai.core.settings import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    KNOWN_PROVIDERS,
    get_configured_provider_keys,
    get_project_settings_path,
    get_user_settings_path,
    mask_api_key,
    resolve_current_settings,
    save_active_model_setting,
    save_custom_endpoint_config,
    save_provider_api_key,
)

_RICH = True

LOCAL_PRESETS = [
    (
        "ollama",
        "Ollama (Local)",
        "http://localhost:11434/v1",
        "ollama",
        "qwen2.5-coder:32b",
        "Local open-source models via Ollama runner",
    ),
    (
        "lmstudio",
        "LM Studio (Local)",
        "http://localhost:1234/v1",
        "lm-studio",
        "local-model",
        "Local LLM GUI server on localhost:1234",
    ),
    (
        "vllm",
        "vLLM / SGLang (Local)",
        "http://localhost:8000/v1",
        "not-needed",
        "default",
        "High-throughput local GPU inference server",
    ),
    (
        "litellm",
        "LiteLLM Proxy",
        "http://localhost:4000/v1",
        "sk-litellm",
        "default",
        "Unified multi-provider proxy on localhost:4000",
    ),
    (
        "groq",
        "Groq (Ultra-fast LPU)",
        "https://api.groq.com/openai/v1",
        "",
        "llama-3.3-70b-versatile",
        "Ultra-low-latency LPU cloud inference (requires GROQ_API_KEY)",
    ),
    (
        "mistral",
        "Mistral AI",
        "https://api.mistral.ai/v1",
        "",
        "mistral-large-latest",
        "Mistral frontier models & Codestral (requires MISTRAL_API_KEY)",
    ),
    (
        "custom",
        "Custom OpenAI-Compatible Endpoint",
        "",
        "",
        "",
        "Specify custom Base URL, API Key, and Model ID",
    ),
]


def _read_input(
    prompt: str,
    default: str = "",
    is_secret: bool = False,
    allow_cancel: bool = True,
) -> str | None:
    """Safely read line or password input with cancel detection."""
    prompt_str = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        if is_secret and sys.stdin.isatty():
            val = getpass.getpass(prompt_str)
            if not val and default:
                return default
            val_clean = val.strip()
            if allow_cancel and val_clean.lower() in ("q", ":q", "cancel", "exit"):
                return None
            return val_clean
        else:
            val = input(prompt_str).strip()
            if not val and default:
                return default
            if allow_cancel and val.lower() in ("q", ":q", "cancel", "exit"):
                return None
            return val
    except (EOFError, KeyboardInterrupt):
        return None if allow_cancel else default


def prompt_save_scope(console: Any | None = None) -> Literal["user", "project"] | None:
    """Prompt user to choose whether to save configuration globally or to project workspace."""
    user_path = get_user_settings_path()
    proj_path = get_project_settings_path(".")

    items = [
        (
            "user",
            "User Global (~/.coderai/settings.json)",
            f"Available across all your repositories on this machine ({user_path})",
        ),
        (
            "project",
            "Project Local (.coderai/settings.json)",
            f"Scoped strictly to this current workspace ({proj_path})",
        ),
        (
            "cancel",
            "Cancel / Do Not Save",
            "Abort saving configuration and return to previous menu",
        ),
    ]

    res = select_with_arrows(
        console,
        items,
        title="Where would you like to save this configuration?",
        default_idx=0,
        allow_cancel=True,
    )
    if res is None or res == 2:
        return None
    if isinstance(res, int) and res == 1:
        return "project"
    return "user"


def render_setup_status_table(console: Any | None, project_root: str = ".") -> None:
    """Display an overview table of all providers, API keys status, and endpoints."""
    keys_status = get_configured_provider_keys(project_root)
    resolved = resolve_current_settings(project_root)
    active_model = resolved.get("model", DEFAULT_MODEL)
    active_base_url = resolved.get("baseURL", DEFAULT_BASE_URL)

    if console is not None and _RICH:
        table = Table(
            title="CoderAI Provider & API Key Status",
            border_style="cyan",
            header_style="bold cyan",
            expand=True,
        )
        table.add_column("Provider", style="bold white", width=18)
        table.add_column("Env Variable", style="dim cyan", width=22)
        table.add_column("Status / Key", width=24)
        table.add_column("Endpoint", style="dim", width=36)
        table.add_column("Default Model", style="yellow")

        for key, p in keys_status.items():
            if p["configured"]:
                status_text = f"[bold green]✓ {p['masked_key']}[/]"
            else:
                status_text = "[dim red]✗ Not configured[/]"

            is_active_model = p["default_model"] == active_model or active_model in p.get(
                "models", []
            )
            name_text = f"[bold white]{p['name']}[/]"
            if is_active_model:
                name_text = f"[bold green]●[/] {name_text} [bold yellow](Active)[/]"

            table.add_row(
                name_text,
                p["env_var"],
                status_text,
                p["default_base_url"][:35],
                p["default_model"],
            )

        console.print()
        console.print(table)

        # Extra info panel
        user_path = get_user_settings_path()
        proj_path = get_project_settings_path(project_root)
        info_lines = [
            f"[bold cyan]Active Model:[/] [bold yellow]{active_model}[/]",
            f"[bold cyan]Active Base URL:[/] [dim]{active_base_url}[/]",
            f"[bold cyan]User Config:[/] [dim]{user_path}[/]",
            f"[bold cyan]Project Config:[/] [dim]{proj_path}[/]",
        ]
        panel = Panel(
            "\n".join(info_lines),
            title="[bold cyan]Active Configuration Overview[/]",
            border_style="bright_blue",
            padding=(0, 1),
        )
        console.print(panel)
        console.print()
    else:
        print("\n--- CoderAI Provider & API Key Status ---")
        for key, p in keys_status.items():
            status = p["masked_key"] if p["configured"] else "Not configured"
            print(f"  {p['name']:<20} [{p['env_var']}] : {status}")
        print(f"  Active Model: {active_model}")
        print(f"  Active Base URL: {active_base_url}\n")


def configure_provider_key_interactive(
    console: Any | None,
    provider_key: str | None = None,
    project_root: str = ".",
) -> str | None:
    """Prompt user to configure or update an API key for a specific provider."""
    keys_status = get_configured_provider_keys(project_root)

    # 1. Select provider if not passed
    if not provider_key or provider_key not in KNOWN_PROVIDERS:
        items = []
        for pkey, p in KNOWN_PROVIDERS.items():
            status_info = keys_status.get(pkey, {})
            is_conf = status_info.get("configured", False)
            masked = status_info.get("masked_key", "Not Set")
            st = (
                f"[bold green]Configured ({masked})[/]" if is_conf else "[dim red]Not configured[/]"
            )
            items.append((pkey, f"{p['name']:<18} — {st}", f"Env: {p['env_var']} • {p['doc_url']}"))
        items.append(("cancel", "Cancel / Return", "Back to previous menu without modifying keys"))

        res = select_with_arrows(
            console,
            items,
            title="Select Provider to Configure API Key",
            default_idx=0,
            allow_cancel=True,
        )
        if res is None or (isinstance(res, int) and items[res][0] == "cancel"):
            if console is not None and _RICH:
                console.print("  [dim]Key configuration cancelled.[/dim]")
            return None

        if isinstance(res, int) and 0 <= res < len(items) - 1:
            provider_key = items[res][0]
        else:
            return None

    info = KNOWN_PROVIDERS[provider_key]
    current_key_raw = keys_status.get(provider_key, {}).get("raw_key") or ""
    current_masked = mask_api_key(current_key_raw)

    if console is not None and _RICH:
        console.print()
        console.print(f"  [bold cyan]Configuring API Key for:[/] [bold yellow]{info['name']}[/]")
        if current_key_raw:
            console.print(f"  [dim]Current Key:[/] [bold green]{current_masked}[/]")
        if info["doc_url"]:
            console.print(f"  [dim]Get an API key at:[/] [underline cyan]{info['doc_url']}[/]")
        console.print("  [dim](Press Enter to keep current, type 'cancel' or 'q' to abort)[/]")
        console.print()

    new_key = _read_input(f"Enter {info['env_var']}", default="", allow_cancel=True)
    if new_key is None:
        if console is not None and _RICH:
            console.print("  [dim]Key configuration cancelled.[/dim]")
        return None
    if not new_key:
        if current_key_raw:
            if console is not None and _RICH:
                console.print("  [dim]Key unchanged.[/dim]")
            return current_key_raw
        else:
            if console is not None and _RICH:
                console.print("  [yellow]No key entered. Configuration aborted.[/dim]")
            return None

    scope = prompt_save_scope(console)
    if scope is None:
        if console is not None and _RICH:
            console.print("  [dim]Saving cancelled. Key not stored.[/dim]")
        return None

    saved_var = save_provider_api_key(provider_key, new_key, scope=scope, project_root=project_root)
    clear_client_pool()

    if console is not None and _RICH:
        console.print(
            f"  [bold green]✓ Successfully saved {saved_var} ({mask_api_key(new_key)}) to {scope} configuration![/]"
        )
    else:
        print(f"Successfully saved {saved_var} to {scope} configuration.")

    return new_key


def configure_custom_endpoint_interactive(
    console: Any | None,
    project_root: str = ".",
) -> str | None:
    """Prompt user to configure a local/custom OpenAI-compatible endpoint."""
    items = [
        (preset[0], f"{preset[1]:<24}", f"{preset[5]} ({preset[2] or 'custom'})")
        for preset in LOCAL_PRESETS
    ]
    items.append(
        ("cancel", "Cancel / Return", "Back to previous menu without configuring endpoint")
    )

    res = select_with_arrows(
        console,
        items,
        title="Select Local / Custom LLM Endpoint Preset",
        default_idx=0,
        allow_cancel=True,
    )
    if res is None or (isinstance(res, int) and items[res][0] == "cancel"):
        if console is not None and _RICH:
            console.print("  [dim]Custom endpoint configuration cancelled.[/dim]")
        return None

    if not isinstance(res, int) or res < 0 or res >= len(LOCAL_PRESETS):
        return None

    chosen = LOCAL_PRESETS[res]
    tag, name, default_url, default_key, default_mod, desc = chosen

    if console is not None and _RICH:
        console.print()
        console.print(f"  [bold cyan]Configuring Custom Endpoint:[/] [bold yellow]{name}[/]")
        console.print(f"  [dim]{desc}[/]\n")

    base_url = _read_input(
        "Base URL (e.g. http://localhost:11434/v1)", default=default_url, allow_cancel=True
    )
    if not base_url:
        if console is not None and _RICH:
            console.print("  [dim]Base URL entry cancelled. Aborting.[/dim]")
        return None

    api_key = _read_input(
        "API Key (enter 'not-needed' or press enter for local)",
        default=default_key or "not-needed",
        allow_cancel=True,
    )
    if api_key is None:
        if console is not None and _RICH:
            console.print("  [dim]Configuration cancelled.[/dim]")
        return None

    model_name = _read_input(
        "Default Model Identifier", default=default_mod or "default", allow_cancel=True
    )
    if model_name is None:
        if console is not None and _RICH:
            console.print("  [dim]Configuration cancelled.[/dim]")
        return None

    scope = prompt_save_scope(console)
    if scope is None:
        if console is not None and _RICH:
            console.print("  [dim]Saving cancelled. Endpoint not stored.[/dim]")
        return None

    save_custom_endpoint_config(
        provider_name=tag,
        base_url=base_url,
        api_key=api_key,
        default_model=model_name,
        scope=scope,
        project_root=project_root,
    )
    clear_client_pool()

    if console is not None and _RICH:
        console.print(
            f"\n  [bold green]✓ Custom endpoint configured successfully![/]\n"
            f"    [dim]Base URL:[/] [bold cyan]{base_url}[/]\n"
            f"    [dim]Model:[/]    [bold yellow]{model_name}[/]\n"
            f"    [dim]Scope:[/]    [bold white]{scope}[/]\n"
        )
    else:
        print(f"Custom endpoint configured: {base_url} -> {model_name} ({scope})")

    return model_name


def select_and_save_model_interactive(
    console: Any | None,
    project_root: str = ".",
) -> str:
    """Prompt user to select a default model and save it to configuration."""
    resolved = resolve_current_settings(project_root)
    current_model = resolved.get("model", DEFAULT_MODEL)

    items: list[tuple[str, str, str]] = []
    default_idx = 0
    for idx, (name, desc, category) in enumerate(CURATED_MODELS):
        badges = get_model_badges(name)
        badges_str = " ".join(f"[{b}]" for b in badges)
        items.append((name, f"{name:<20} {badges_str}", f"[{category}] {desc}"))
        if name == current_model:
            default_idx = idx

    items.append(
        (
            "cancel",
            "Cancel (Keep Current Model)",
            f"Retain '{current_model}' and return without changes",
        )
    )

    res = select_with_arrows(
        console,
        items,
        title=f"Select Default Model (Current: {current_model})",
        default_idx=default_idx,
        allow_custom=True,
        allow_cancel=True,
    )

    if res is None or (isinstance(res, int) and items[res][0] == "cancel"):
        if console is not None and _RICH:
            console.print(
                f"  [dim]Model selection cancelled. Retaining current model '{current_model}'.[/dim]"
            )
        return current_model

    chosen_model = current_model
    if isinstance(res, int) and 0 <= res < len(CURATED_MODELS):
        chosen_model = CURATED_MODELS[res][0]
    elif isinstance(res, str) and res.strip():
        val = res.strip()
        from coderai.cli.fuzzy import fuzzy_filter

        model_names = [name for name, _, _ in CURATED_MODELS]
        fuzzy_models = fuzzy_filter(val, model_names, limit=1)
        if fuzzy_models:
            chosen_model = fuzzy_models[0]
        else:
            chosen_model = val

    if chosen_model != current_model:
        scope = prompt_save_scope(console)
        if scope is None:
            if console is not None and _RICH:
                console.print(f"  [dim]Saving cancelled. Retaining '{current_model}'.[/dim]")
            return current_model

        save_active_model_setting(chosen_model, scope=scope, project_root=project_root)
        clear_client_pool()
        if console is not None and _RICH:
            console.print(
                f"  [bold green]✓ Default active model set to:[/] [bold yellow]{chosen_model}[/] [dim]({scope})[/]"
            )
        else:
            print(f"Default active model set to: {chosen_model} ({scope})")

    return chosen_model


def run_connectivity_test_interactive(
    console: Any | None,
    project_root: str = ".",
    model_override: str | None = None,
) -> bool:
    """Run a live connectivity and authentication test for the active or specified model."""
    resolved = resolve_current_settings(project_root)
    active_model = model_override or resolved.get("model", DEFAULT_MODEL)
    base_url = resolved.get("baseURL")
    api_key = resolved.get("apiKey")

    if console is not None and _RICH:
        console.print(f"\n  [bold cyan]Testing connection to:[/] [bold yellow]{active_model}[/]")
        console.print("  [dim]Sending probe request...[/]")

    success, message = probe_provider_connectivity(
        model=active_model,
        base_url=base_url,
        api_key=api_key,
        timeout=12.0,
    )

    if console is not None and _RICH:
        if success:
            console.print("\n  [bold green]✓ Connection Verified![/]")
            console.print(f"    [dim]{message}[/]\n")
        else:
            console.print("\n  [bold red]✗ Connection Failed![/]")
            console.print(f"    [red]{message}[/]")
            console.print(
                "    [dim]Tip: Check your API key or endpoint settings via '/setup keys'.[/dim]\n"
            )
    else:
        status_sym = "✓" if success else "✗"
        print(f"\n[{status_sym}] {message}\n")

    return success


def run_quick_setup_wizard(
    console: Any | None,
    project_root: str = ".",
    mgr: Any | None = None,
) -> None:
    """Guided 3-step quick onboarding setup wizard."""
    if console is not None and _RICH:
        panel = Panel(
            "[bold white]Welcome to CoderAI Setup Wizard[/]\n"
            "[dim]Let's get your AI coding environment ready in 3 simple steps.[/]",
            title="[bold cyan]CoderAI Quick Setup[/]",
            border_style="cyan",
            padding=(0, 2),
        )
        console.print()
        console.print(panel)
        console.print()

    # Step 1: Provider selection
    provider_options = [
        (
            "openai",
            "OpenAI (GPT-5.6 Sol / Luna / o3-mini)",
            "Official OpenAI frontier models & reasoning",
        ),
        (
            "deepseek",
            "DeepSeek (V4 Pro / Flash / Reasoner)",
            "1M context, state-of-the-art coding & low cost",
        ),
        (
            "gemini",
            "Google Gemini (3.7 Flash / 2.5 Pro)",
            "Next-gen hybrid reasoning & multi-modal",
        ),
        ("anthropic", "Anthropic Claude (3.7 Sonnet)", "Frontier tool use & deep hybrid reasoning"),
        (
            "openrouter",
            "OpenRouter (All Providers Aggregator)",
            "Unified API key access to 100+ models",
        ),
        (
            "local",
            "Local / Custom Endpoint (Ollama, LM Studio, vLLM)",
            "Run locally on your own GPU/CPU offline",
        ),
        ("cancel", "Cancel / Exit Quick Setup", "Return to previous menu"),
    ]

    res = select_with_arrows(
        console,
        provider_options,
        title="Step 1/3: Select your primary LLM Provider",
        default_idx=0,
        allow_cancel=True,
    )
    if res is None or (isinstance(res, int) and provider_options[res][0] == "cancel"):
        if console is not None and _RICH:
            console.print("  [dim]Quick setup cancelled.[/dim]\n")
        return

    if not isinstance(res, int) or res < 0 or res >= len(provider_options) - 1:
        return

    chosen_provider_tag = provider_options[res][0]

    # Step 2: Handle key / endpoint input
    if chosen_provider_tag == "local":
        configured_mod = configure_custom_endpoint_interactive(console, project_root)
        if not configured_mod:
            if console is not None and _RICH:
                console.print("  [dim]Quick setup cancelled.[/dim]\n")
            return
    else:
        key_res = configure_provider_key_interactive(console, chosen_provider_tag, project_root)
        if not key_res:
            if console is not None and _RICH:
                console.print("  [dim]Quick setup cancelled.[/dim]\n")
            return

    # Step 3: Select default model
    if chosen_provider_tag != "local":
        if console is not None and _RICH:
            console.print("\n  [bold cyan]Step 2/3: Select Default Active Model[/]")
        chosen_model = select_and_save_model_interactive(console, project_root)
    else:
        resolved = resolve_current_settings(project_root)
        chosen_model = resolved.get("model", DEFAULT_MODEL)

    # Step 4: Test Connection
    if console is not None and _RICH:
        console.print("  [bold cyan]Step 3/3: Validating Provider Connection...[/]")
    run_connectivity_test_interactive(console, project_root, model_override=chosen_model)

    # If active session manager is attached, update its model dynamically
    if mgr is not None:
        if hasattr(mgr, "set_model"):
            mgr.set_model(chosen_model)

    if console is not None and _RICH:
        console.print(
            "  [bold green]● Setup complete![/] You are ready to start pair-programming with CoderAI.\n"
        )


def run_setup_wizard(
    console: Any | None,
    project_root: str = ".",
    mgr: Any | None = None,
    initial_subcommand: str | None = None,
) -> None:
    """Main interactive setup wizard entry point."""
    sub = (initial_subcommand or "").strip().lower()

    if sub in ("keys", "key", "auth"):
        configure_provider_key_interactive(console, project_root=project_root)
        return
    elif sub in ("models", "model"):
        chosen = select_and_save_model_interactive(console, project_root=project_root)
        if mgr is not None and hasattr(mgr, "set_model"):
            mgr.set_model(chosen)
        return
    elif sub in ("custom", "local", "endpoint", "provider"):
        configure_custom_endpoint_interactive(console, project_root=project_root)
        return
    elif sub in ("test", "ping", "check"):
        run_connectivity_test_interactive(console, project_root=project_root)
        return
    elif sub in ("status", "show", "list"):
        render_setup_status_table(console, project_root=project_root)
        return
    elif sub in ("quick", "start", "init"):
        run_quick_setup_wizard(console, project_root=project_root, mgr=mgr)
        return

    # Full interactive main menu loop
    while True:
        menu_items = [
            (
                "quick",
                "Quick Guided Setup (Recommended)",
                "3-step setup walkthrough to configure provider, key, and model",
            ),
            (
                "keys",
                "Configure Provider API Keys",
                "Manage keys for OpenAI, DeepSeek, Google Gemini, Anthropic, OpenRouter",
            ),
            (
                "model",
                "Select Default Active Model",
                "Choose or customize your default model for coding sessions",
            ),
            (
                "local",
                "Configure Local / Custom LLM Endpoint",
                "Set up Ollama, LM Studio, vLLM, Groq, LiteLLM, or custom proxy",
            ),
            (
                "test",
                "Test API Key & Model Connectivity",
                "Verify live connection and authentication with active model",
            ),
            (
                "status",
                "View Configuration & Keys Overview",
                "Display current endpoints, masked keys, and config locations",
            ),
            (
                "exit",
                "Exit Setup Wizard",
                "Exit setup wizard and return to CoderAI prompt (Esc / q)",
            ),
        ]

        res = select_with_arrows(
            console,
            menu_items,
            title="CoderAI Setup & Configuration Manager",
            default_idx=0,
            allow_cancel=True,
        )

        if res is None or not isinstance(res, int) or res == len(menu_items) - 1:
            if console is not None and _RICH:
                console.print("  [dim]Exited CoderAI setup.[/dim]\n")
            break

        selected_key = menu_items[res][0]

        if selected_key == "quick":
            run_quick_setup_wizard(console, project_root=project_root, mgr=mgr)
        elif selected_key == "keys":
            configure_provider_key_interactive(console, project_root=project_root)
        elif selected_key == "model":
            chosen = select_and_save_model_interactive(console, project_root=project_root)
            if mgr is not None and hasattr(mgr, "set_model"):
                mgr.set_model(chosen)
        elif selected_key == "local":
            configure_custom_endpoint_interactive(console, project_root=project_root)
        elif selected_key == "test":
            run_connectivity_test_interactive(console, project_root=project_root)
        elif selected_key == "status":
            render_setup_status_table(console, project_root=project_root)
        elif selected_key == "exit":
            if console is not None and _RICH:
                console.print("  [dim]Exited CoderAI setup.[/dim]\n")
            break


def run_setup_cli(args: Any, project_root: str = ".") -> int:
    """Execute setup from the CLI outside of the interactive REPL."""
    console = Console()

    # Check for non-interactive flags
    provider = getattr(args, "setup_provider", None)
    key = getattr(args, "setup_key", None)
    model = getattr(args, "setup_model", None)
    base_url = getattr(args, "setup_base_url", None)
    scope = "project" if getattr(args, "setup_project", False) else "user"
    do_test = getattr(args, "setup_test", False)
    do_status = getattr(args, "setup_status", False)

    if do_status:
        render_setup_status_table(console, project_root=project_root)
        return 0

    if do_test:
        success = run_connectivity_test_interactive(
            console, project_root=project_root, model_override=model
        )
        return 0 if success else 1

    if provider and key:
        saved_var = save_provider_api_key(provider, key, scope=scope, project_root=project_root)
        clear_client_pool()
        console.print(
            f"[bold green]✓ Successfully configured {provider} ({saved_var}) in {scope} settings.[/]"
        )
        if model:
            save_active_model_setting(model, scope=scope, project_root=project_root)
            console.print(f"[bold green]✓ Set active model to {model} ({scope}).[/]")
        return 0

    if base_url:
        save_custom_endpoint_config(
            provider_name=provider or "custom",
            base_url=base_url,
            api_key=key or "not-needed",
            default_model=model or "default",
            scope=scope,
            project_root=project_root,
        )
        clear_client_pool()
        console.print(
            f"[bold green]✓ Configured endpoint {base_url} (model: {model or 'default'}) in {scope} settings.[/]"
        )
        return 0

    if model and not (provider or base_url or key):
        save_active_model_setting(model, scope=scope, project_root=project_root)
        clear_client_pool()
        console.print(f"[bold green]✓ Set active model to {model} ({scope}).[/]")
        return 0

    # Otherwise launch interactive wizard
    run_setup_wizard(console, project_root=project_root)
    return 0
