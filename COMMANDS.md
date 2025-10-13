# CoderAI Commands Reference

This document provides a comprehensive reference for all available commands in CoderAI.

## Table of Contents
- [CLI Commands](#cli-commands)
- [Interactive Chat Commands](#interactive-chat-commands)
- [Configuration Commands](#configuration-commands)
- [History Commands](#history-commands)

---

## CLI Commands

These commands are run from your terminal as `coderAI <command>`.

### Basic Usage

```bash
# Start interactive chat
coderAI chat

# Start chat with specific model
coderAI chat -m gpt-5

# Resume previous session
coderAI chat -r SESSION_ID

# Ask a single question (single-shot mode)
coderAI ask "How do I create a Python virtual environment?"

# Quick prompt (shorthand)
coderAI "explain this code"
```

### Model Management

#### `coderAI models`
List all available models and providers.

```bash
coderAI models
```

Shows:
- OpenAI models (gpt-5, gpt-5-mini, gpt-5-nano)
- LM Studio provider
- Current default model

#### `coderAI set-model <model_name>`
Set the default model for new sessions.

```bash
coderAI set-model gpt-5-mini
coderAI set-model lmstudio
```

Valid models: `gpt-5`, `gpt-5-mini`, `gpt-5-nano`, `lmstudio`

### System Management

#### `coderAI status`
Show system status and diagnostics.

```bash
coderAI status
```

Displays:
- Configuration directory
- Default model
- API key status (OpenAI)
- LM Studio endpoint
- History statistics

#### `coderAI info`
Show information about the agent and current model.

```bash
coderAI info
```

Displays:
- CoderAI version
- Model information
- Available tools

#### `coderAI setup`
Run the interactive setup wizard.

```bash
coderAI setup
```

Configures:
- OpenAI API key
- Default model
- LM Studio endpoint

---

## Interactive Chat Commands

These commands are used within an active chat session (after running `coderAI chat`). All commands start with `/`.

### General Commands

#### `/help`
Display help message with all available commands.

```
/help
```

#### `/exit` or `/quit`
Exit the chat session.

```
/exit
```

#### `/clear`
Clear the screen.

```
/clear
```

### Context Management

#### `/clear-context`
Clear the conversation context and start fresh while staying in the same session.

```
/clear-context
```

Use this when you want to start a new conversation without exiting the chat.

#### `/history`
Show conversation history for the current session.

```
/history
```

Displays all messages with role (user/assistant) and truncated content.

### Model Management

#### `/model`
Show current model information.

```
/model
```

Displays detailed information about the currently active model.

#### `/change-model`
Change the model/provider during the chat session.

```
/change-model
```

Follow the prompts to select a new model. Type `cancel` to abort.

#### `/providers`
Show available LLM providers and their features.

```
/providers
```

Lists:
- OpenAI Provider (features, requirements)
- LM Studio Provider (features, requirements)

### Session Management

#### `/save`
Manually save the current session.

```
/save
```

Sessions are automatically saved, but you can force a save with this command.

#### `/status`
Show current session status.

```
/status
```

Displays:
- Session ID
- Current model
- Provider
- Message count
- Streaming status
- Save history status
- Session timestamps

#### `/export`
Export the conversation to a JSON file.

```
/export
```

Creates a timestamped JSON file with:
- Session ID
- Model name
- All messages
- Timestamp

### Tools & Configuration

#### `/tools`
List all available tools.

```
/tools
```

Shows all tools the agent can use (filesystem, git, search, terminal, etc.).

#### `/config`
Show current configuration.

```
/config
```

Displays all configuration values (API keys are masked).

#### `/tokens`
Show token usage information.

```
/tokens
```

Displays:
- Total messages
- Total characters
- Approximate token count

---

## Configuration Commands

Manage CoderAI configuration settings.

### `coderAI config show`
Display current configuration.

```bash
coderAI config show
```

### `coderAI config set <key> <value>`
Set a configuration value.

```bash
coderAI config set default_model gpt-5-mini
coderAI config set temperature 0.7
coderAI config set max_tokens 4096
coderAI config set streaming true
coderAI config set save_history false
```

Common configuration keys:
- `openai_api_key` - Your OpenAI API key
- `default_model` - Default model to use
- `temperature` - Model temperature (0.0 - 2.0)
- `max_tokens` - Maximum tokens per response
- `streaming` - Enable streaming (true/false)
- `save_history` - Save conversation history (true/false)
- `lmstudio_endpoint` - LM Studio endpoint URL
- `lmstudio_model` - LM Studio model name

### `coderAI config reset`
Reset configuration to defaults.

```bash
coderAI config reset
```

---

## History Commands

Manage conversation history.

### `coderAI history list`
List all conversation sessions.

```bash
coderAI history list
```

### `coderAI history delete <session_id>`
Delete a specific session.

```bash
coderAI history delete abc123
```

### `coderAI history clear`
Clear all conversation history.

```bash
coderAI history clear
```

**Note:** This command requires confirmation.

---

## Quick Reference

### Most Common Commands

| Command | Description |
|---------|-------------|
| `coderAI chat` | Start interactive chat |
| `coderAI models` | List available models |
| `coderAI status` | Check system status |
| `/help` | Show help in chat |
| `/clear-context` | Clear conversation |
| `/change-model` | Switch model |
| `/export` | Export conversation |
| `/exit` | Exit chat |

### Workflow Examples

**Start a new session:**
```bash
coderAI chat
```

**Switch models mid-conversation:**
```
/change-model
gpt-5
```

**Clear context and start fresh:**
```
/clear-context
```

**Export your conversation:**
```
/export
```

**Check system status:**
```bash
coderAI status
```

---

## Tips

1. **Model Selection**: Use `gpt-5-mini` for most tasks (balanced performance and cost), `gpt-5` for complex tasks, and `lmstudio` for privacy-focused local inference.

2. **Context Management**: Use `/clear-context` when switching topics to avoid confusion from previous conversation context.

3. **Session Management**: Sessions are auto-saved if `save_history` is enabled. Use `/save` to force save at any time.

4. **Token Awareness**: Use `/tokens` periodically to monitor conversation length. Large contexts can slow responses and increase costs.

5. **Export Important Conversations**: Use `/export` to save important conversations for future reference.

---

## Environment Variables

You can also configure CoderAI using environment variables:

```bash
export OPENAI_API_KEY="your-api-key"
export CODERAI_DEFAULT_MODEL="gpt-5-mini"
export CODERAI_TEMPERATURE="0.7"
export CODERAI_MAX_TOKENS="4096"
export LMSTUDIO_ENDPOINT="http://localhost:1234/v1"
```

Environment variables take precedence over configuration file settings.

