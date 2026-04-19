# CoderAI Architecture

This document describes the architecture and design of CoderAI.

## Overview

CoderAI is a sophisticated coding agent CLI tool built with Python, featuring:
- Multiple LLM backend support (OpenAI, Anthropic Claude, LM Studio, Ollama)
- MCP (Model Context Protocol) tools for various operations
- Rich terminal UI with syntax highlighting
- Interactive chat plus supporting CLI utility commands
- Persistent conversation history and configuration

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                         CLI Layer                            │
│  (coderAI/cli.py - Click commands, argument parsing)        │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────┴────────────────────────────────────┐
│                      Agent Layer                             │
│  (coderAI/agent.py - Orchestrates LLM and tools)           │
│  - Message handling                                          │
│  - Tool call execution                                       │
│  - Session management                                        │
└─────┬─────────────────┬────────────────────┬────────────────┘
      │                 │                    │
      │                 │                    │
┌─────┴────┐     ┌──────┴───────┐    ┌──────┴─────────┐
│   LLM    │     │    Tools     │    │      UI        │
│ Providers│     │   Registry   │    │   Components   │
└──────────┘     └──────────────┘    └────────────────┘
```

## Component Details

### 1. CLI Layer (`coderAI/cli.py`)

**Responsibility:** Command-line interface and user interaction

**Components:**
- Click-based command structure
- Command groups: chat, ask, config, history, info, setup, models, set-model, status, cost, tasks
- Argument parsing and validation
- Entry point management

**Key Functions:**
- `main()` - Entry point
- `chat()` - Interactive mode
- `ask()` - Single-shot mode
- `config()` - Configuration management
- `history()` - Session management

### 2. Agent Layer (`coderAI/agent.py`)

**Responsibility:** Core orchestration logic

**Components:**
- `Agent` class - Main orchestrator
- Message loop management
- Tool call handling
- LLM provider integration

**Key Methods:**
- `process_message()` - Handle user messages
- `_stream_response()` - Stream LLM responses
- `_execute_tool()` - Execute tool calls
- Session management

**Flow:**
```
User Message → Agent → LLM → Tool Calls → Execute Tools → 
→ Tool Results → LLM → Final Response → User
```

### 3. LLM Providers (`coderAI/llm/`)

**Responsibility:** Abstract different LLM backends

**Structure:**
```
llm/
├── base.py         # Abstract LLMProvider
├── openai.py       # OpenAI GPT-5, o1, o3-mini variants
├── anthropic.py    # Anthropic Claude models
├── lmstudio.py     # LM Studio local models
└── ollama.py       # Ollama local models
```

**Base Interface:**
```python
class LLMProvider:
    async def chat(messages, tools) -> Response
    async def stream(messages, tools) -> AsyncIterator
    def count_tokens(text) -> int
    def supports_tools() -> bool
```

**Implementations:**
- `OpenAIProvider` - OpenAI API with tiktoken (GPT-5, o1, o3-mini)
- `AnthropicProvider` - Anthropic API (Claude models)
- `LMStudioProvider` - OpenAI-compatible local API
- `OllamaProvider` - Ollama local API

### 4. Tools (`coderAI/tools/`)

**Responsibility:** MCP tool implementations

**Structure:**
```
tools/
├── base.py         # Tool interface and registry
├── filesystem.py   # File operations (read, write, search_replace, apply_diff)
├── terminal.py     # Command execution
├── git.py          # Git operations
├── search.py       # Text search and grep
├── web.py          # Web search and URL reading
├── memory.py       # Knowledge base
├── mcp.py          # MCP server integration
├── undo.py         # File backup and rollback
├── project.py      # Project context detection
├── context_manage.py  # Context pinning
├── tasks.py        # Task/TODO management
├── subagent.py     # Sub-agent delegation
├── lint.py         # Linter integration
└── vision.py       # Image reading
```

**Tool Interface:**
```python
class Tool:
    name: str
    description: str
    
    async def execute(**kwargs) -> Dict
    def get_schema() -> Dict
    def get_parameters() -> Dict
```

**Tool Registry:**
- Registers all available tools
- Provides schemas for LLM function calling
- Routes tool executions

**Available Tools:**

*Filesystem:*
- `read_file` - Read file contents
- `write_file` - Create/overwrite files
- `search_replace` - Edit files
- `apply_diff` - Apply unified diff patches
- `list_directory` - List directory contents
- `glob_search` - Find files by pattern

*Terminal:*
- `run_command` - Execute shell commands
- `run_background` - Start background processes

*Git:*
- `git_add` - Stage files
- `git_status` - Repository status
- `git_diff` - View changes
- `git_commit` - Create commits
- `git_log` - View history

*Search:*
- `text_search` - Text-based codebase search
- `grep` - Pattern matching with regex

*Web:*
- `web_search` - Internet search
- `read_url` - Fetch URL content

*Memory:*
- `save_memory` - Store information
- `recall_memory` - Retrieve information

*Additional:*
- `project_context` - Auto-detect project type and structure
- `manage_context` - Pin files to context
- `manage_tasks` - Task/TODO tracking
- `delegate_task` - Spawn sub-agent for isolated tasks
- Persona frontmatter uses high-level tool labels like `Read`, `Edit`, and `Bash`; these expand to concrete tool IDs, while read-only tools remain available across personas
- `lint` - Run project linter
- `read_image` - Read and analyze images
- `undo` / `undo_history` - File rollback
- `mcp_connect` / `mcp_call_tool` / `mcp_list` - MCP integration

### 5. UI Components (`coderAI/ui/`)

**Responsibility:** Terminal UI using Rich

**Structure:**
```
ui/
├── display.py      # Rich display utilities
├── interactive.py  # Interactive chat
└── streaming.py    # Streaming handler
```

**Display Features:**
- Markdown rendering
- Syntax-highlighted code
- Colored messages (info, success, error, warning)
- Tables and trees
- Panels and separators
- Progress indicators

**Interactive Chat:**
- Prompt with history
- Command handling (/help, /clear, etc.)
- Session context management
- Error handling

**Streaming:**
- Live updating display
- Token-by-token rendering
- Tool call accumulation

### 6. Configuration (`coderAI/config.py`)

**Responsibility:** Configuration management

**Features:**
- JSON-based config file (`~/.coderAI/config.json`)
- Environment variable support
- Pydantic validation
- Sensitive data masking

**Settings:**
- API keys (OpenAI, Anthropic)
- Model preferences
- Temperature, max_tokens, reasoning_effort
- LM Studio and Ollama endpoints
- Streaming, history options

### 7. History (`coderAI/history.py`)

**Responsibility:** Conversation persistence

**Features:**
- Session-based storage
- JSON serialization
- Message history
- Session metadata

**Structure:**
```
~/.coderAI/history/
└── session_TIMESTAMP_ID.json
```

## Data Flow

### Interactive Mode

```
1. User launches: coderAI chat
2. Agent creates/loads session
3. Interactive UI displays welcome
4. Loop:
   a. User enters message
   b. Agent processes message
   c. LLM generates response (streaming)
   d. If tool calls needed:
      - Display tool calls
      - Execute tools
      - Display results
      - Loop back to LLM
   e. Display final response
   f. Save session
5. User exits
```

### Utility Commands

```
1. User runs a CLI utility command (for example `coderAI info`)
2. CLI creates any required agent/config state
3. Command prints structured output
4. Process exits
```

### Tool Execution Flow

```
1. LLM returns tool_calls in response
2. Agent parses tool calls
3. For each tool:
   a. Extract name and arguments
   b. Display tool call info
   c. Execute via ToolRegistry
   d. Display result
   e. Add result to message history
4. Send results back to LLM
5. Get final response
```

## Design Patterns

### 1. Abstract Factory Pattern
- `LLMProvider` base class
- Different implementations: OpenAI, Anthropic, LM Studio, Ollama

### 2. Registry Pattern
- `ToolRegistry` manages tools
- Dynamic tool registration

### 3. Strategy Pattern
- Different streaming strategies
- Interactive chat vs utility command flows

### 4. Command Pattern
- CLI commands as discrete operations
- Tool executions as commands

### 5. Observer Pattern
- Streaming responses with live updates
- Display updates based on events

## Technology Stack

**Core:**
- Python 3.9+
- asyncio for concurrency

**LLM Integration:**
- OpenAI Python SDK (OpenAI provider)
- aiohttp for Anthropic, LM Studio, and Ollama APIs
- tiktoken for token counting (OpenAI)

**CLI:**
- Click for command structure
- prompt-toolkit for interactive input

**UI:**
- Rich for terminal formatting
- Live display for streaming

**Data:**
- Pydantic for validation
- JSON for persistence

**Tools:**
- subprocess for commands
- pathlib for file operations
- aiohttp for web requests

## Security Considerations

1. **API Key Storage:** 
   - Stored in config file with restricted permissions
   - Can use environment variables
   - Masked in display

2. **Command Execution:**
   - Shell commands run with user permissions
   - No privilege escalation
   - Timeout limits

3. **File Operations:**
   - Respect user permissions
   - No automatic deletion
   - Path validation

4. **Network Requests:**
   - Timeout limits
   - Error handling
   - No credential leakage

## Performance Optimizations

1. **Async/Await:**
   - Non-blocking I/O
   - Concurrent tool execution (potential)

2. **Streaming:**
   - Real-time response display
   - Reduced perceived latency

3. **Token Management:**
   - Token counting to avoid limits
   - Context window management

4. **Caching:**
   - Config loaded once
   - Tool registry created once

## Extensibility

### Adding New Tools

```python
from coderAI.tools.base import Tool

class MyTool(Tool):
    name = "my_tool"
    description = "Does something"
    
    def get_parameters(self):
        return {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    
    async def execute(self, **kwargs):
        # Implementation
        return {"success": True, ...}

# Register in agent.py
registry.register(MyTool())
```

### Adding New LLM Provider

```python
from coderAI.llm.base import LLMProvider

class MyProvider(LLMProvider):
    async def chat(self, messages, tools, **kwargs):
        # Implementation
        pass
    
    async def stream(self, messages, tools, **kwargs):
        # Implementation
        pass
    
    def count_tokens(self, text):
        # Implementation
        pass
```

### Adding New Commands

```python
@cli.command()
@click.option(...)
def my_command(...):
    """My command description."""
    # Implementation
```

## Error Handling

1. **LLM Errors:**
   - API failures caught and reported
   - Retry logic implemented for transient errors (timeouts, rate limits, 5xx)
   - Fallback messages

2. **Tool Errors:**
   - Each tool execution wrapped in try/except
   - Error details in result
   - Displayed to user

3. **Configuration Errors:**
   - Validation with Pydantic
   - Default values provided
   - Clear error messages

4. **User Errors:**
   - Input validation
   - Helpful error messages
   - Recovery suggestions

## Testing Strategy

1. **Unit Tests:**
   - Individual tool testing
   - Provider mocking
   - Configuration testing

2. **Integration Tests:**
   - End-to-end flows
   - Tool execution
   - Session persistence
   - Manual live-provider harnesses in `manual_subagent_delegation.py` and `manual_parallel_subagents.py`

3. **Installation Test:**
   - `test_installation.py` validates setup
   - Checks all imports
   - Verifies components

4. **Automated Entry Point:**
   - `pytest` is the supported automated test runner
   - `make test` wraps `pytest`, the installation smoke test, and a basic CLI version check

## Future Enhancements

1. **Enhanced Tool Support:**
   - Database operations
   - API integrations
   - Image generation

2. **Multi-Agent:**
   - Specialized agents
   - Agent collaboration
   - Task delegation

3. **Plugin System:**
   - External tool plugins
   - Custom provider plugins

4. **Advanced Features:**
   - Code execution sandbox
   - Notebook integration
   - IDE plugins

5. **Performance:**
   - Parallel tool execution
   - Response caching
   - Smart context pruning

