# CoderAI - Intelligent Coding Agent CLI

A powerful coding agent CLI tool similar to Claude Code and Gemini CLI, featuring MCP (Model Context Protocol) tools, beautiful Rich terminal UI, and support for multiple LLM backends.

## Features

- **Rich Terminal UI**: Beautiful syntax highlighting, progress indicators, tables, and panels
- **Multiple LLM Support**: OpenAI (GPT-5, o1, o3-mini), Anthropic Claude, LM Studio, and Ollama local models
- **Dynamic Model Switching**: Change models mid-conversation without losing context
- **Comprehensive MCP Tools**:
  - File operations (read, write, search, replace)
  - Terminal command execution
  - Git operations (status, diff, commit, log)
  - Codebase search (text search and grep)
  - Web search for documentation
  - Knowledge base and memory
- **Advanced Session Management**:
  - Persistent conversation history
  - Context clearing and resetting
  - Session export to JSON
  - Resume previous sessions
- **Streaming Responses**: Real-time token-by-token display
- **Rich Command System**: 15+ interactive commands for full control
- **System Diagnostics**: Status monitoring, token usage, and configuration viewing

## Installation

```bash
pip3 install -e .
```

## Quick Start

```bash
# Interactive mode
coderAI chat

# Use specific model
coderAI --model gpt-5-mini chat

# Use local LM Studio
coderAI --model lmstudio chat

# Resume previous session
coderAI --resume SESSION_ID
```

## Configuration

Configure API keys and preferences:

```bash
# Set API keys (as needed for your providers)
coderAI config set openai_api_key YOUR_API_KEY
coderAI config set anthropic_api_key YOUR_ANTHROPIC_KEY

# Show current configuration
coderAI config show

# Set default model
coderAI config set default_model gpt-5-mini

# Configure LM Studio endpoint (provide your server URL)
coderAI config set lmstudio_endpoint http://localhost:1234/v1

# Configure LM Studio model name (optional)
coderAI config set lmstudio_model your-model-name
```

Configuration is stored at `~/.coderAI/config.json`. You can also use environment variables:

- `OPENAI_API_KEY`: OpenAI API key
- `ANTHROPIC_API_KEY`: Anthropic API key (for Claude models)
- `LMSTUDIO_ENDPOINT`: LM Studio API endpoint (e.g., http://localhost:1234/v1)
- `OLLAMA_ENDPOINT`: Ollama API endpoint (e.g., http://localhost:11434/v1)
- `CODERAI_DEFAULT_MODEL`: Default model to use

## Commands

### CLI Commands

```bash
# Basic Usage
coderAI chat                    # Interactive mode
coderAI --model MODEL chat      # Use specific model

# Model Management
coderAI models                  # List available models
coderAI set-model gpt-5-mini    # Set default model

# Configuration
coderAI config set KEY VALUE    # Set configuration
coderAI config show             # Show configuration
coderAI config reset            # Reset to defaults

# History Management
coderAI history list            # List conversation sessions
coderAI history delete ID       # Delete specific session
coderAI history clear           # Clear all history

# System
coderAI status                  # Show system status
coderAI info                    # Show agent info
coderAI setup                   # Run setup wizard
coderAI cost                    # Show API cost tracking
coderAI tasks list               # List project tasks
coderAI --version               # Show version
coderAI --help                  # Show help
```

### Interactive Chat Commands

Inside a chat session, use these commands (starting with `/`):

```bash
/help            # Show help message
/clear           # Clear screen
/clear-context   # Clear conversation context
/change-model    # Switch model/provider
/model           # Show current model info
/providers       # List available providers
/status          # Show session status
/tools           # List available tools
/config          # Show configuration
/tokens          # Show token usage
/save            # Manually save session
/export          # Export conversation to JSON
/history         # Show conversation history
/exit            # Exit chat
```

**📚 For detailed documentation, see [COMMANDS.md](COMMANDS.md)**

## Available Models

**OpenAI:** `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `o1`, `o1-mini`, `o3-mini`  
**Anthropic:** `claude-4-sonnet`, `claude-3.5-sonnet`, `claude-3.5-haiku`, `claude-3-opus`  
**Local:** `lmstudio`, `ollama`  

Run `coderAI models` to see the full list with descriptions.

## Examples

### Interactive Mode

```bash
$ coderAI chat
CoderAI> Create a Python web server using Flask

[Agent proceeds to create files, install dependencies, etc.]
```

### Using Local Models (LM Studio)

```bash
# 1. Start LM Studio and note your server URL (usually http://localhost:1234/v1)

# 2. Configure the endpoint:
$ coderAI config set lmstudio_endpoint http://YOUR_SERVER_URL:PORT/v1

# 3. (Optional) Set model name if needed:
$ coderAI config set lmstudio_model your-model-name

# 4. Start using LM Studio:
$ coderAI --model lmstudio chat
```

### Using Local Models (Ollama)

```bash
# 1. Install and start Ollama, then pull a model: ollama pull llama3

# 2. (Optional) Configure if not using defaults:
$ coderAI config set ollama_endpoint http://localhost:11434/v1
$ coderAI config set ollama_model llama3

# 3. Start using Ollama:
$ coderAI --model ollama chat
```

## License

MIT License
