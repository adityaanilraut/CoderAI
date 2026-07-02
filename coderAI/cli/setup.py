"""CLI setup wizard."""

import click

from coderAI.system.config import config_manager
from .utils import valid_models, valid_endpoint

_REASONING_CHOICES = ("high", "medium", "low", "none")


@click.command()
def setup() -> None:
    """Interactive setup wizard."""
    from coderAI.ui.display import display

    display.print_header("CoderAI Setup Wizard")
    display.print()

    captured_any_key = False

    api_key_providers = [
        (1, "OpenAI", "Required for using GPT models", "openai_api_key"),
        (2, "Anthropic", "Required for using Claude models", "anthropic_api_key"),
        (
            3,
            "Groq",
            "Required for using Groq models (including openai/gpt-oss-120b and openai/gpt-oss-20b)",
            "groq_api_key",
        ),
        (4, "DeepSeek", "Required for using DeepSeek models", "deepseek_api_key"),
        (5, "Gemini", "Required for using Gemini models", "gemini_api_key"),
    ]
    for num, label, desc, config_key in api_key_providers:
        display.print(f"[bold]{num}. {label} API Key[/bold]")
        display.print(f"   {desc}")
        while True:
            key = click.prompt(
                f"   Enter your {label} API key (or press Enter to skip)",
                default="",
                show_default=False,
                hide_input=True,
            )
            if not key:
                break
            if len(key) < 20:
                display.print_error("   API key seems too short (minimum 20 characters).")
                continue
            if label == "OpenAI" and not key.startswith("sk-"):
                display.print_error("   OpenAI API keys should start with 'sk-'.")
                continue
            if label == "Anthropic" and not key.startswith("sk-ant-"):
                display.print_error("   Anthropic API keys should start with 'sk-ant-'.")
                continue
            config_manager.set(config_key, key)
            display.print_success(f"   {label} API key saved")
            captured_any_key = True
            break
        display.print()

    valid = valid_models()
    display.print("[bold]6. Default Model[/bold]")
    display.print("   Run `coderAI models` after setup for the full list.")
    display.print(
        "   Common: claude-sonnet-4-6, opus, haiku, gpt-5.4-mini, "
        "gpt-5.4, deepseek-v4-flash, gemini-3.5-flash, lmstudio, ollama"
    )
    while True:
        model = click.prompt("   Enter default model", default="claude-4-sonnet").strip()
        if model in valid:
            config_manager.set("default_model", model)
            display.print_success(f"   Default model set to {model}")
            break
        display.print_error(f"   Unknown model: {model}. Run `coderAI models` for the full list.")
    display.print()

    display.print("[bold]7. Reasoning Effort[/bold]")
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

    display.print("[bold]8. LM Studio Configuration (Optional)[/bold]")
    display.print("   For using local models with LM Studio")
    use_lmstudio = click.confirm("   Configure LM Studio?", default=False)
    if use_lmstudio:
        while True:
            endpoint = click.prompt(
                "   LM Studio server URL", default="http://localhost:1234/v1"
            ).strip()
            if valid_endpoint(endpoint):
                config_manager.set("lmstudio_endpoint", endpoint)
                break
            display.print_error("   Endpoint must be a full http(s)://host:port/v1 URL.")
        model_name = click.prompt(
            "   LM Studio model name (optional)",
            default="local-model",
            show_default=True,
        )
        config_manager.set("lmstudio_model", model_name)
        display.print_success("   LM Studio configuration saved")
    display.print()

    display.print("[bold]9. Ollama Configuration (Optional)[/bold]")
    display.print("   For using local models with Ollama")
    use_ollama = click.confirm("   Configure Ollama?", default=False)
    if use_ollama:
        while True:
            endpoint = click.prompt(
                "   Ollama server URL", default="http://localhost:11434/v1"
            ).strip()
            if valid_endpoint(endpoint):
                config_manager.set("ollama_endpoint", endpoint)
                break
            display.print_error("   Endpoint must be a full http(s)://host:port/v1 URL.")
        model_name = click.prompt("   Ollama model name", default="llama3", show_default=True)
        config_manager.set("ollama_model", model_name)
        display.print_success("   Ollama configuration saved")
    display.print()

    display.print("[bold]10. Web Search Configuration (Optional)[/bold]")
    display.print("   For querying the web during execution.")
    use_web = click.confirm("   Configure Web Search?", default=False)
    if use_web:
        backend = click.prompt(
            "   Select search backend",
            type=click.Choice(["none", "ddg", "tavily", "exa", "searxng"], case_sensitive=False),
            default="ddg",
        ).lower()
        if backend != "none":
            config_manager.set("search_backend", backend)
            if backend == "tavily":
                tavily_key = click.prompt(
                    "   Enter your Tavily API key",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
                if tavily_key:
                    config_manager.set("tavily_api_key", tavily_key)
                    captured_any_key = True
            elif backend == "exa":
                exa_key = click.prompt(
                    "   Enter your Exa API key",
                    default="",
                    show_default=False,
                    hide_input=True,
                )
                if exa_key:
                    config_manager.set("exa_api_key", exa_key)
                    captured_any_key = True

            rate_limit = click.prompt(
                "   Enter domain rate limit delay in seconds",
                type=float,
                default=1.0,
            )
            config_manager.set("rate_limit_delay_seconds", rate_limit)

            concurrent = click.confirm("   Enable concurrent search (DDG + SearXNG)?", default=True)
            config_manager.set("concurrent_search", concurrent)
            display.print_success("   Web Search configuration saved")
    display.print()

    display.print("[bold]11. MCP Servers Configuration (Optional)[/bold]")
    display.print("    Add Model Context Protocol servers to provide custom tools.")
    use_mcp = click.confirm("    Configure MCP servers?", default=False)
    if use_mcp:
        from coderAI.tools.mcp import load_mcp_servers, mcp_servers_path, save_mcp_servers

        mcp_data = load_mcp_servers()
        mcp_data.setdefault("mcpServers", {})

        def _prompt_mcp_url(example: str) -> str:
            while True:
                url: str = click.prompt(f"    Enter URL (e.g. {example})").strip()
                if valid_endpoint(url):
                    return url
                display.print_error("    Invalid URL — must start with http:// or https://")

        while True:
            server_name = click.prompt(
                "    Enter MCP server name (or press Enter to finish)",
                default="",
                show_default=False,
            ).strip()
            if not server_name:
                break

            transport = click.prompt(
                "    Transport type",
                type=click.Choice(["stdio", "sse", "http"]),
                default="stdio",
            )
            if transport == "sse":
                url = _prompt_mcp_url("http://localhost:8080/sse")
                mcp_data["mcpServers"][server_name] = {
                    "transport": "sse",
                    "url": url,
                }
            elif transport == "http":
                url = _prompt_mcp_url("https://mcp.example.com/mcp")
                entry: dict = {"transport": "http", "url": url}
                auth = click.prompt(
                    "    Authorization header (optional, e.g. 'Bearer TOKEN')", default=""
                ).strip()
                if auth:
                    entry["headers"] = {"Authorization": auth}
                    display.print_warning(
                        f"    Heads up: this token is stored in plaintext in "
                        f"{mcp_servers_path()}. For OAuth-protected servers, prefer "
                        "`coderAI mcp login` instead of pasting a static token."
                    )
                mcp_data["mcpServers"][server_name] = entry
            else:
                command = click.prompt("    Enter server command (e.g. npx, python3)").strip()
                args_str = click.prompt(
                    "    Enter command arguments (comma-separated, optional)", default=""
                ).strip()
                args = [a.strip() for a in args_str.split(",") if a.strip()] if args_str else []
                mcp_data["mcpServers"][server_name] = {
                    "command": command,
                    "args": args,
                }
            display.print_success(f"    Configured MCP server '{server_name}'")

        try:
            save_mcp_servers(mcp_data)
            display.print_success(f"    MCP server configurations saved to {mcp_servers_path()}")
        except Exception as e:
            display.print_error(f"    Failed to save MCP servers config: {e}")
    display.print()

    if not captured_any_key and not (use_lmstudio or use_ollama):
        display.print_warning(
            "No API keys entered and no local provider configured. "
            "`coderAI chat` will refuse to start until one is set — re-run "
            "`coderAI setup` or set an env var (ANTHROPIC_API_KEY, etc.)."
        )
    else:
        display.print_success("Setup complete! Run 'coderAI chat' to start.")
