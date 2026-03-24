"""Interactive chat interface with Rich UI."""

import sys
import os
from pathlib import Path
from typing import Callable, Optional, List, Set

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.completion import WordCompleter
from rich.panel import Panel

from .display import display
from ..config import config_manager
from ..events import event_emitter
from ..agent_tracker import agent_tracker


def _setup_event_listeners():
    """Subscribe display handlers to agent events."""
    event_emitter.on("tool_call", lambda tool_name, arguments: display.print_tool_call(tool_name, arguments))
    event_emitter.on("tool_error", lambda tool_name, error: display.print_error(f"Tool '{tool_name}' error: {error}"))
    event_emitter.on("tool_result", lambda tool_name, result: display.print_tool_result(tool_name, result))
    event_emitter.on("agent_status", lambda message: display.print(message))
    event_emitter.on("agent_error", lambda message: display.print_error(message))
    event_emitter.on("agent_warning", lambda message: display.print_warning(message))
    
    # State for the active loading spinner
    status_state = {"active": None}
    
    def handle_status_start(message):
        if status_state["active"]:
            status_state["active"].stop()
        status_state["active"] = display.status(message)
        status_state["active"].start()
        
    def handle_status_stop(*args, **kwargs):
        if status_state["active"]:
            status_state["active"].stop()
            status_state["active"] = None

    def handle_agent_lifecycle(action, info):
        if action == "finished":
            display.print_agent_completion(info)

    event_emitter.on("status_start", handle_status_start)
    event_emitter.on("status_stop", handle_status_stop)
    event_emitter.on("agent_lifecycle", handle_agent_lifecycle)

# NOTE: _setup_event_listeners() is called lazily from InteractiveChat.__init__
# to avoid side effects on import.


class InteractiveChat:
    """Interactive chat interface."""
    _listeners_initialized = False

    def __init__(self):
        """Initialize interactive chat."""
        # Lazily initialize event listeners (once per process, not at import time)
        if not InteractiveChat._listeners_initialized:
            _setup_event_listeners()
            InteractiveChat._listeners_initialized = True

        self.history = InMemoryHistory()

        # Available commands for auto-completion
        self.commands = [
            "/help", "/clear", "/clear-context", "/history", "/model",
            "/change-model", "/reasoning", "/config", "/tools", "/save", "/sessions", 
            "/resume", "/tokens", "/export", "/status", "/providers", 
            "/plan", "/compact", "/auto-approve", "/skills", "/skill", "/agent",
            "/agents", "/stop", "/exit", "/quit"
        ]
        self.completer = WordCompleter(self.commands, ignore_case=True)

        self.session = PromptSession(
            history=self.history,
            completer=self.completer,
            complete_while_typing=True
        )

        # Custom style for prompt
        self.style = Style.from_dict(
            {
                "prompt": "#00aa00 bold",
            }
        )


    def _get_project_structure(self, max_depth: int = 2, max_files: int = 50) -> str:
        """Get a string representation of the project structure.
        
        Args:
            max_depth: Maximum recursion depth
            max_files: Maximum number of files to list
            
        Returns:
            String with file tree
        """
        start_path = Path(".")
        output = ["Project Structure:"]
        file_count = 0
        
        # Directories to ignore
        ignore_dirs = {
            ".git", "__pycache__", "node_modules", "venv", ".venv", 
            ".idea", ".vscode", "dist", "build", ".egg-info",
            "coverage", ".pytest_cache", ".mypy_cache"
        }
        
        # Files to ignore
        ignore_files = {
            ".DS_Store", "package-lock.json", "yarn.lock", "poetry.lock"
        }

        def _add_dir(path: Path, prefix: str = "", current_depth: int = 0):
            nonlocal file_count
            if current_depth > max_depth or file_count >= max_files:
                return

            try:
                # Sort contents: directories first, then files
                entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                
                # Filter entries
                entries = [
                    e for e in entries 
                    if e.name not in ignore_dirs and e.name not in ignore_files
                    and not e.name.startswith(".")
                ]

                for i, entry in enumerate(entries):
                    if file_count >= max_files:
                        if file_count == max_files:
                            output.append(f"{prefix}  ... (limit reached)")
                            file_count += 1
                        return

                    is_last = (i == len(entries) - 1)
                    connector = "└── " if is_last else "├── "
                    
                    if entry.is_dir():
                        output.append(f"{prefix}{connector}{entry.name}/")
                        new_prefix = prefix + ("    " if is_last else "│   ")
                        _add_dir(entry, new_prefix, current_depth + 1)
                    else:
                        output.append(f"{prefix}{connector}{entry.name}")
                        file_count += 1
                        
            except PermissionError:
                output.append(f"{prefix}└── [Permission Denied]")

        _add_dir(start_path)
        return "\n".join(output)

    def print_welcome(self, model: str):
        """Print welcome message."""
        welcome_text = f"""
[bold cyan]CoderAI - Intelligent Coding Agent[/bold cyan]

Model: [yellow]{model}[/yellow]
Type your message or command. Press Ctrl+C or type 'exit' to quit.

[dim]Available commands:
  /help           - Show this help message
  /clear          - Clear the screen
  /clear-context  - Clear conversation context (start fresh)
  /history        - Show conversation history
  /model          - Show current model info
  /change-model   - Change model/provider
  /reasoning      - Change reasoning effort (for o1, gpt-5, claude 3.7+/4)
  /config         - Show current configuration
  /tools          - List available tools
  /save           - Manually save current session
  /sessions       - List saved conversation sessions
  /resume <id>    - Resume a saved session
  /tokens         - Show token usage info
  /export         - Export conversation to file
  /status         - Show current session status
  /providers      - Show available LLM providers
  /plan           - Plan a task step-by-step before executing
  /compact        - Manually compact/summarize context history
  /auto-approve   - Toggle auto-approve for tool execution
  /skills         - List available skills in .coderAI/skills/
  /skill <name>   - Load and execute a specific skill
  /agent [name]   - Switch to a specific agent persona, or list them
  /agents         - Show all active/recent agents and their status
  /stop [id]      - Stop a running agent (or all if no id given)
  /exit           - Exit the chat[/dim]
        """
        display.print_panel(welcome_text.strip(), title="Welcome", border_style="cyan")
        display.print()

    async def get_input(self, prompt_text: str = "You") -> Optional[str]:
        """Get user input with rich prompt.

        Args:
            prompt_text: Text to show in prompt

        Returns:
            User input or None if EOF/exit
        """
        try:
            user_input = await self.session.prompt_async(
                f"{prompt_text}> ",
                style=self.style,
            )
            return user_input.strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def print_assistant_response(self, content: str):
        """Print assistant's response."""
        if content:
            display.print("\n[bold blue]Assistant:[/bold blue]")
            display.print_markdown(content)
            display.print()

    def handle_command(self, command: str, context: dict) -> bool:
        """Handle special commands.

        Args:
            command: Command string (starting with /)
            context: Context dictionary with session info

        Returns:
            True if should exit, False otherwise
        """
        if command == "/help":
            self.print_welcome(context.get("model", "unknown"))
            return False

        elif command == "/clear":
            display.clear()
            return False

        elif command == "/clear-context":
            # Clear conversation context — full reset
            if context.get("agent"):
                agent = context["agent"]
                agent.session = None
                agent.create_session()
                agent.context_manager.clear()
                agent.total_prompt_tokens = 0
                agent.total_completion_tokens = 0
                agent.total_tokens = 0
                agent.cost_tracker = agent.cost_tracker.__class__()
                context["messages"] = []
                display.print_success("Conversation context cleared. Starting fresh!")
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/history":
            if context.get("messages"):
                display.print_header("Conversation History")
                for i, msg in enumerate(context["messages"], 1):
                    # Handle both dict and Message object types
                    if hasattr(msg, 'role'):  # Message object
                        role = msg.role
                        content = msg.content
                    elif isinstance(msg, dict):  # Dictionary
                        role = msg.get("role", "unknown")
                        content = msg.get("content", "")
                    else:
                        continue
                    
                    # Skip system messages in history display
                    if role == "system":
                        continue
                    
                    if content:
                        display.print(f"\n[bold]{i}. {role.title()}:[/bold]")
                        display.print(content[:200] + ("..." if len(content) > 200 else ""))
                display.print()
            else:
                display.print_info("No conversation history yet")
            return False

        elif command == "/model":
            model_info = context.get("model_info", {})
            display.print_tree(model_info, "Model Information")
            return False

        elif command == "/change-model":
            # Change model/provider
            from ..llm.openai import OpenAIProvider
            from ..llm.anthropic import AnthropicProvider
            from ..llm.groq import GroqProvider
            from ..llm.deepseek import DeepSeekProvider
            
            display.print_header("Available Models")
            for model_name in OpenAIProvider.SUPPORTED_MODELS:
                display.print(f"  • [cyan]{model_name}[/cyan] (OpenAI)")
            for model_name in AnthropicProvider.SUPPORTED_MODELS:
                display.print(f"  • [cyan]{model_name}[/cyan] (Anthropic)")
            for model_name in GroqProvider.SUPPORTED_MODELS:
                display.print(f"  • [cyan]{model_name}[/cyan] (Groq)")
            for model_name in DeepSeekProvider.SUPPORTED_MODELS:
                display.print(f"  • [cyan]{model_name}[/cyan] (DeepSeek)")
            display.print(f"  • [cyan]lmstudio[/cyan] - Local LM Studio model")
            display.print(f"  • [cyan]ollama[/cyan] - Local Ollama model")
            display.print("\nType the model name in your next message to switch (or 'cancel' to cancel)")
            context["awaiting_model_change"] = True
            return False

        elif command == "/reasoning":
            # Change reasoning effort
            display.print_header("Reasoning Effort")
            display.print("How much thinking reasoning models (e.g. o1, gpt-5, claude-3.7-sonnet, claude-4-sonnet) should use.")
            display.print("  • [cyan]high[/cyan]   - Max reasoning tokens")
            display.print("  • [cyan]medium[/cyan] - Default balanced reasoning")
            display.print("  • [cyan]low[/cyan]    - Fast/cheap reasoning")
            display.print("  • [cyan]none[/cyan]   - Disabled (where supported)")
            display.print("\nType the effort level in your next message to switch (or 'cancel' to cancel)")
            context["awaiting_reasoning_change"] = True
            return False

        elif command == "/config":
            # Show configuration
            config_data = config_manager.show()
            display.print_tree(config_data, "Current Configuration")
            return False

        elif command == "/tools":
            # List available tools
            if context.get("agent"):
                display.print_header("Available Tools")
                tools = context["agent"].tools.get_all()
                for tool in tools:
                    display.print(f"\n[cyan]• {tool.name}[/cyan]")
                    display.print(f"  {tool.description}")
                display.print()
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/save":
            # Save session
            if context.get("agent"):
                context["agent"].save_session()
                session_id = context["agent"].session.session_id if context["agent"].session else "unknown"
                display.print_success(f"Session saved: {session_id}")
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/tokens":
            # Show token usage info
            agent = context.get("agent")
            if agent and hasattr(agent, "total_tokens") and agent.total_tokens > 0:
                # Use real token counts from the provider
                display.print_header("Token Usage (from LLM provider)")
                display.print(f"Prompt tokens:     [yellow]{agent.total_prompt_tokens:,}[/yellow]")
                display.print(f"Completion tokens: [yellow]{agent.total_completion_tokens:,}[/yellow]")
                display.print(f"Total tokens:      [yellow]{agent.total_tokens:,}[/yellow]")
                
                # Show exact dollar cost and budget
                from ..cost import CostTracker
                cost_usd = agent.cost_tracker.get_total_cost()
                display.print(f"Session Cost:      [green]{CostTracker.format_cost(cost_usd)}[/green]")
                if agent.config.budget_limit > 0:
                    display.print(f"Budget Limit:      [blue]{CostTracker.format_cost(agent.config.budget_limit)}[/blue]")
            elif context.get("messages"):
                total_messages = len(context["messages"])
                
                # Handle both dict and Message object types
                total_chars = 0
                for msg in context["messages"]:
                    if hasattr(msg, 'content'):  # Message object
                        total_chars += len(str(msg.content))
                    elif isinstance(msg, dict):  # Dictionary
                        total_chars += len(str(msg.get("content", "")))
                
                approx_tokens = total_chars // 4  # Rough approximation
                
                display.print_header("Token Usage Information")
                display.print(f"Total messages: [yellow]{total_messages}[/yellow]")
                display.print(f"Total characters: [yellow]{total_chars}[/yellow]")
                display.print(f"Approx tokens: [yellow]{approx_tokens}[/yellow]")
                display.print(f"\n[dim]Note: This is a rough approximation. Actual token count may vary.[/dim]")
            else:
                display.print_info("No messages yet")
            return False

        elif command == "/sessions":
            # List all saved sessions
            from ..history import history_manager
            sessions = history_manager.list_sessions()
            if not sessions:
                display.print_info("No saved sessions found.")
                return False
                
            display.print_header("Saved Sessions")
            for summary in sessions[:20]:  # Show up to 20 recent sessions
                display.print(f"[cyan]• {summary['session_id']}[/cyan] ({summary['created_at']})")
                display.print(f"  Model: [yellow]{summary['model']}[/yellow]")
                display.print(f"  Messages: {summary['messages']}")
                display.print()
            
            display.print("[dim]Use /resume <session_id> to load a session[/dim]")
            return False

        elif command.startswith("/resume"):
            parts = command.split(" ", 1)
            if len(parts) < 2:
                display.print_warning("Please provide a session ID (e.g., /resume abc-123)")
                return False
            
            session_id = parts[1].strip()
            if not context.get("agent"):
                display.print_warning("No agent context available to load session into.")
                return False
                
            try:
                session = context["agent"].load_session(session_id)
                if session:
                    context["messages"] = session.get_messages_for_api()
                    context["model"] = session.model
                    display.print_success(f"Successfully loaded session: {session_id}")
                    # Re-instantiate provider if the model changed
                    context["agent"].model = session.model
                    context["agent"].provider = context["agent"]._create_provider()
                else:
                    display.print_warning(f"Session not found or invalid: {session_id}")
            except Exception as e:
                display.print_error(f"Failed to load session: {str(e)}")
            
            return False

        elif command == "/export":
            # Export conversation
            if context.get("agent") and context.get("messages"):
                import json
                from datetime import datetime
                
                session_id = context["agent"].session.session_id if context["agent"].session else "unknown"
                filename = f"coderAI_export_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                
                # Convert Message objects to dicts for JSON serialization
                messages_data = []
                for msg in context["messages"]:
                    if hasattr(msg, 'model_dump'):  # Pydantic Message object
                        messages_data.append(msg.model_dump())
                    elif isinstance(msg, dict):
                        messages_data.append(msg)
                
                export_data = {
                    "session_id": session_id,
                    "model": context.get("model", "unknown"),
                    "timestamp": datetime.now().isoformat(),
                    "messages": messages_data
                }
                
                try:
                    with open(filename, "w") as f:
                        json.dump(export_data, f, indent=2)
                    display.print_success(f"Conversation exported to: {filename}")
                except Exception as e:
                    display.print_error(f"Failed to export: {str(e)}")
            else:
                display.print_info("No conversation to export")
            return False

        elif command == "/status":
            # Show session status
            if context.get("agent"):
                agent = context["agent"]
                session = agent.session
                
                display.print_header("Current Session Status")
                display.print(f"Session ID: [cyan]{session.session_id if session else 'None'}[/cyan]")
                display.print(f"Model: [yellow]{context.get('model', 'unknown')}[/yellow]")
                display.print(f"Provider: [yellow]{agent.provider.__class__.__name__}[/yellow]")
                display.print(f"Messages: [cyan]{len(context.get('messages', []))}[/cyan]")
                display.print(f"Streaming: [cyan]{agent.streaming}[/cyan]")
                display.print(f"Auto-approve: [cyan]{agent.auto_approve}[/cyan]")
                display.print(f"Save History: [cyan]{agent.config.save_history}[/cyan]")
                
                # Show token usage and cost
                if hasattr(agent, "total_tokens") and agent.total_tokens > 0:
                    display.print(f"\n[bold]Token Usage:[/bold]")
                    display.print(f"  Prompt tokens:     [yellow]{agent.total_prompt_tokens:,}[/yellow]")
                    display.print(f"  Completion tokens: [yellow]{agent.total_completion_tokens:,}[/yellow]")
                    display.print(f"  Total tokens:      [yellow]{agent.total_tokens:,}[/yellow]")
                    from ..cost import CostTracker
                    cost_usd = agent.cost_tracker.get_total_cost()
                    display.print(f"  Session Cost:      [green]{CostTracker.format_cost(cost_usd)}[/green]")
                    if agent.config.budget_limit > 0:
                        display.print(f"  Budget Limit:      [blue]{CostTracker.format_cost(agent.config.budget_limit)}[/blue]")
                
                if session:
                    from datetime import datetime
                    created_str = datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M:%S")
                    updated_str = datetime.fromtimestamp(session.updated_at).strftime("%Y-%m-%d %H:%M:%S")
                    display.print(f"\n[dim]Session created: {created_str}[/dim]")
                    display.print(f"[dim]Last updated: {updated_str}[/dim]")
            else:
                display.print_warning("No agent context available")
            return False

        elif command.startswith("/plan"):
            parts = command.split(" ", 1)
            context["plan_mode"] = True
            if len(parts) > 1 and parts[1].strip():
                display.print_info("Plan mode enabled.")
                context["_pending_prompt"] = parts[1].strip()
            else:
                display.print_info("Plan mode enabled. Enter your task in the next message.")
            return False

        elif command == "/providers":
            # Show available providers
            from ..llm.openai import OpenAIProvider
            from ..llm.anthropic import AnthropicProvider
            from ..llm.groq import GroqProvider
            from ..llm.deepseek import DeepSeekProvider

            display.print_header("Available LLM Providers")
            
            display.print("\n[bold cyan]OpenAI Provider[/bold cyan]")
            display.print(f"  Models: {', '.join(OpenAIProvider.SUPPORTED_MODELS)}")
            display.print("  Features: Function calling, streaming, reasoning")
            display.print("  Requires: OpenAI API key")
            
            display.print("\n[bold cyan]Anthropic Provider[/bold cyan]")
            display.print(f"  Models: {', '.join(AnthropicProvider.SUPPORTED_MODELS)}")
            display.print("  Features: Function calling, streaming, extended thinking")
            display.print("  Requires: Anthropic API key")
            
            display.print("\n[bold cyan]Groq Provider[/bold cyan]")
            display.print(f"  Models: {', '.join(GroqProvider.SUPPORTED_MODELS)}")
            display.print("  Features: Function calling, streaming, fast inference")
            display.print("  Requires: Groq API key")
            
            display.print("\n[bold cyan]DeepSeek Provider[/bold cyan]")
            display.print(f"  Models: {', '.join(DeepSeekProvider.SUPPORTED_MODELS)}")
            display.print("  Features: Function calling, streaming, reasoning")
            display.print("  Requires: DeepSeek API key")
            
            display.print("\n[bold cyan]LM Studio Provider[/bold cyan]")
            display.print("  Models: Local models via LM Studio")
            display.print("  Features: Local inference, privacy")
            display.print("  Requires: LM Studio running locally")
            
            display.print("\n[bold cyan]Ollama Provider[/bold cyan]")
            display.print("  Models: Local models via Ollama")
            display.print("  Features: Local inference, privacy, easy model management")
            display.print("  Requires: Ollama running locally")
            
            display.print("\n[dim]Use /change-model to switch between providers[/dim]")
            display.print()
            return False

        elif command == "/compact":
            if context.get("agent"):
                agent = context["agent"]
                display.print_info("Compacting context...")
                try:
                    messages = agent.session.get_messages_for_api() if agent.session else []
                    if len(messages) <= 2:
                        display.print_info("Context is already small, nothing to compact.")
                        return False

                    context["_pending_compact"] = True
                except Exception as e:
                    display.print_error(f"Failed to compact context: {e}")
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/auto-approve":
            # Toggle auto-approve mode
            if context.get("agent"):
                agent = context["agent"]
                agent.auto_approve = not agent.auto_approve
                state = "[bold green]ON[/bold green]" if agent.auto_approve else "[bold red]OFF[/bold red]"
                display.print(f"Auto-approve is now {state}")
                if agent.auto_approve:
                    display.print_warning(
                        "Tools will execute without confirmation. "
                        "Use /auto-approve again to disable."
                    )
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/skills":
            skills_dir = Path(".coderAI/skills")
            if not skills_dir.exists():
                display.print_info("No .coderAI/skills/ directory found. Create one and add .md files to use skills.")
                return False
                
            skills = list(skills_dir.glob("*.md"))
            if not skills:
                display.print_info("No skills found in .coderAI/skills/.")
                return False
                
            display.print_header("Available Skills")
            for skill in skills:
                display.print(f"  • [cyan]{skill.stem}[/cyan]")
            display.print("\n[dim]Use /skill <name> to load a skill[/dim]")
            return False

        elif command.startswith("/skill "):
            parts = command.split(" ", 2)
            if len(parts) < 2:
                display.print_warning("Please provide a skill name (e.g., /skill tdd-workflow)")
                return False
                
            skill_name = parts[1].strip()
            additional_context = parts[2].strip() if len(parts) > 2 else "Please execute this skill."
            skills_dir = Path(".coderAI/skills")
            
            # Check for name.md or exact match
            skill_file = skills_dir / f"{skill_name}.md"
            if not skill_file.exists():
                # Allow referencing deeply nested SKILL.md files like in everything-claude-code
                skill_file = skills_dir / skill_name / "SKILL.md"
                if not skill_file.exists():
                    display.print_error(f"Skill '{skill_name}' not found. Searched for '{skill_name}.md' and '{skill_name}/SKILL.md'")
                    return False
            
            try:
                skill_content = skill_file.read_text()
                display.print_success(f"Loaded skill: {skill_name}")
                
                # Push the skill into context as an instruction to execute
                context["execute_skill"] = skill_content
                context["_pending_prompt"] = additional_context
            except Exception as e:
                display.print_error(f"Failed to read skill file: {e}")
                
            return False

        elif command.startswith("/agent"):
            parts = command.split(" ", 1)
            from ..agents import get_available_personas, load_agent_persona
            
            # Use the agent's project root so personas are found correctly
            project_root = "."
            if context.get("agent") and hasattr(context["agent"], "config"):
                project_root = getattr(context["agent"].config, "project_root", ".")
            
            if len(parts) < 2 or not parts[1].strip():
                # List available agents
                display.print_header("Available Agent Personas")
                personas = get_available_personas(project_root)
                if not personas:
                    display.print_info("No agent personas found in .coderAI/agents/")
                else:
                    for p in personas:
                        display.print(f"  • [cyan]{p}[/cyan]")
                display.print("\n[dim]Use /agent <name> to switch persona[/dim]")
                return False
                
            persona_name = parts[1].strip()
            if not context.get("agent"):
                display.print_warning("No agent context available")
                return False
                
            persona = load_agent_persona(persona_name, project_root)
            if persona:
                context["agent"].persona = persona
                display.print_success(f"Switched to agent persona: {persona.name}")
                display.print_info(persona.description)
                
                # Update the LLM model if the persona specifies one and we want to auto-switch
                if persona.model and context["agent"].model != persona.model:
                    # Depending on provider setup, this might require a fresh provider instance
                    old_model = context["agent"].model
                    context["agent"].model = persona.model
                    context["agent"].provider = context["agent"]._create_provider()
                    display.print_info(f"Model switched from {old_model} to {persona.model}")
            else:
                display.print_error(f"Persona '{persona_name}' not found. Searched in .coderAI/agents/ (project_root={project_root})")
            return False

        elif command == "/agents":
            summary = agent_tracker.get_summary()
            if not summary["agents"]:
                display.print_info("No agents have been tracked this session.")
            else:
                display.print_agent_panel(summary["agents"])
                from ..cost import CostTracker
                display.print(
                    f"\n[dim]Totals — Active: {summary['active_count']} | "
                    f"Tokens: {summary['total_tokens']:,} | "
                    f"Cost: {CostTracker.format_cost(summary['total_cost_usd'])}[/dim]"
                )
            return False

        elif command.startswith("/stop"):
            parts = command.split(" ", 1)
            if len(parts) >= 2 and parts[1].strip():
                agent_id_fragment = parts[1].strip()
                found = False
                for info in agent_tracker.get_active():
                    if agent_id_fragment in info.agent_id:
                        agent_tracker.cancel(info.agent_id)
                        display.print_success(f"Cancellation requested for agent '{info.name}' ({info.agent_id[-8:]})")
                        found = True
                        break
                if not found:
                    display.print_warning(f"No active agent matching '{agent_id_fragment}'")
            else:
                active = agent_tracker.get_active()
                if active:
                    agent_tracker.cancel_all()
                    display.print_success(f"Cancellation requested for {len(active)} active agent(s)")
                else:
                    display.print_info("No active agents to stop.")
            return False

        elif command in ["/exit", "/quit"]:
            return True

        else:
            display.print_warning(f"Unknown command: {command}")
            display.print_info("Type /help for available commands")
            return False

    async def run(
        self,
        message_handler: Callable,
        model: str = "gpt-5-mini",
        agent = None,
        initial_messages: list = None,
    ):
        """Run the interactive chat loop.

        Args:
            message_handler: Async function to handle user messages
            model: Model name
            agent: Agent instance
            initial_messages: Initial conversation messages
        """
        self.print_welcome(model)

        context = {
            "model": model,
            "agent": agent,
            "messages": initial_messages or [],
            "model_info": {},
        }

        while True:
            try:
                # Update prompt with current model
                prompt_text = f"You [{context.get('model', 'unknown')}]"
                
                # Get user input
                user_input = await self.get_input(prompt_text)

                if user_input is None:
                    display.print("\n[dim]Goodbye![/dim]")
                    break

                if not user_input:
                    continue

                # Handle commands
                if user_input.startswith("/"):
                    should_exit = self.handle_command(user_input, context)
                    if should_exit:
                        display.print("\n[dim]Goodbye![/dim]")
                        break
                    # Handle async operations requested by commands
                    if context.pop("_pending_compact", False):
                        try:
                            await context["agent"].compact_context()
                        except Exception as e:
                            display.print_error(f"Failed to compact context: {e}")
                    
                    if "_pending_prompt" in context:
                        user_input = context.pop("_pending_prompt")
                        # Fall through to process the message
                    else:
                        continue

                # Process user message
                try:
                    # Handle plan mode: wrap the user input with planning
                    # instructions so the LLM generates a plan instead of
                    # executing directly.
                    if context.get("plan_mode"):
                        context["plan_mode"] = False
                        plan_prompt = (
                            "The user wants you to PLAN the following task "
                            "step-by-step WITHOUT executing anything yet. "
                            "Output a numbered list of concrete steps you would "
                            "take (including which tools you'd use). Do NOT call "
                            "any tools — only describe the plan.\n\n"
                            f"Task: {user_input}\n\n"
                            f"{self._get_project_structure()}"
                        )
                        response = await message_handler(plan_prompt, context)

                        if response:
                            if "messages" in response:
                                context["messages"] = response["messages"]
                            if "model_info" in response:
                                context["model_info"] = response["model_info"]

                        # Ask user whether to execute the plan
                        display.print(
                            "\n[bold cyan]Execute this plan? "
                            "(y/yes to execute, anything else to skip)[/bold cyan]"
                        )
                        context["awaiting_plan_confirmation"] = user_input
                        continue

                    # Handle plan confirmation
                    if context.get("awaiting_plan_confirmation"):
                        original_task = context.pop("awaiting_plan_confirmation")
                        if user_input.lower() in ("y", "yes"):
                            user_input = (
                                f"Execute the following task step by step, using "
                                f"the plan you just created: {original_task}"
                            )
                        else:
                            display.print_info("Plan discarded.")
                            continue

                    # Handle executing an injected skill
                    if context.get("execute_skill"):
                        skill_content = context.pop("execute_skill")
                        user_input = (
                            f"Please execute the following skill workflow:\n\n"
                            f"<skill>\n{skill_content}\n</skill>\n\n"
                            f"Additional context from user: {user_input}"
                        )

                    response = await message_handler(user_input, context)

                    if response:
                        # Update context
                        if "messages" in response:
                            context["messages"] = response["messages"]
                        if "model_info" in response:
                            context["model_info"] = response["model_info"]

                        if context.get("agent"):
                            used, limit = context["agent"].get_context_usage()
                            pct = (used / limit) * 100 if limit > 0 else 0
                            display.print(f"\n[dim]Context usage: {used:,}/{limit:,} tokens ({pct:.1f}%)[/dim]\n")

                except Exception as e:
                    display.print_error(f"Failed to process message: {str(e)}")
                    continue

            except KeyboardInterrupt:
                active = agent_tracker.get_active()
                if active:
                    agent_tracker.cancel_all()
                    display.print(
                        f"\n[bold yellow]Stopping {len(active)} active agent(s)...[/bold yellow] "
                        "[dim]Press Ctrl+C again to force quit.[/dim]"
                    )
                else:
                    display.print("\n[dim]Interrupted. Type /exit to quit.[/dim]")
                continue


# Global interactive chat instance
interactive_chat = InteractiveChat()
