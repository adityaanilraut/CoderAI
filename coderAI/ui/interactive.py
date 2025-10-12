"""Interactive chat interface with Rich UI."""

import sys
from typing import Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.panel import Panel

from .display import display
from ..config import config_manager


class InteractiveChat:
    """Interactive chat interface."""

    def __init__(self):
        """Initialize interactive chat."""
        self.history = InMemoryHistory()
        self.session = PromptSession(history=self.history)

        # Custom style for prompt
        self.style = Style.from_dict(
            {
                "prompt": "#00aa00 bold",
            }
        )

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
  /config         - Show current configuration
  /tools          - List available tools
  /save           - Manually save current session
  /tokens         - Show token usage info
  /export         - Export conversation to file
  /status         - Show current session status
  /providers      - Show available LLM providers
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
            # Clear conversation context
            if context.get("agent"):
                context["agent"].session = None
                context["agent"].create_session()
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
            display.print_header("Available Models")
            display.print("  1. [cyan]gpt-5[/cyan] - OpenAI GPT-5 (most capable)")
            display.print("  2. [cyan]gpt-5-mini[/cyan] - OpenAI GPT-5 Mini (balanced)")
            display.print("  3. [cyan]gpt-5-nano[/cyan] - OpenAI GPT-5 Nano (fast)")
            display.print("  4. [cyan]lmstudio[/cyan] - Local LM Studio model")
            display.print("\nType the model name in your next message to switch (or 'cancel' to cancel)")
            context["awaiting_model_change"] = True
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
            if context.get("messages"):
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
                display.print(f"Save History: [cyan]{agent.config.save_history}[/cyan]")
                
                if session:
                    from datetime import datetime
                    created_str = datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M:%S")
                    updated_str = datetime.fromtimestamp(session.updated_at).strftime("%Y-%m-%d %H:%M:%S")
                    display.print(f"\n[dim]Session created: {created_str}[/dim]")
                    display.print(f"[dim]Last updated: {updated_str}[/dim]")
            else:
                display.print_warning("No agent context available")
            return False

        elif command == "/providers":
            # Show available providers
            display.print_header("Available LLM Providers")
            
            display.print("\n[bold cyan]OpenAI Provider[/bold cyan]")
            display.print("  Models: gpt-5, gpt-5-mini, gpt-5-nano")
            display.print("  Features: Function calling, streaming")
            display.print("  Requires: OpenAI API key")
            
            display.print("\n[bold cyan]LM Studio Provider[/bold cyan]")
            display.print("  Models: Local models via LM Studio")
            display.print("  Features: Local inference, privacy")
            display.print("  Requires: LM Studio running locally")
            
            display.print("\n[dim]Use /change-model to switch between providers[/dim]")
            display.print()
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
                    continue

                # Process user message
                try:
                    response = await message_handler(user_input, context)

                    if response:
                        # Update context
                        if "messages" in response:
                            context["messages"] = response["messages"]
                        if "model_info" in response:
                            context["model_info"] = response["model_info"]

                except Exception as e:
                    display.print_error(f"Failed to process message: {str(e)}")
                    continue

            except KeyboardInterrupt:
                display.print("\n[dim]Interrupted. Type /exit to quit.[/dim]")
                continue


# Global interactive chat instance
interactive_chat = InteractiveChat()

