# New Features & Commands Added to CoderAI

## Summary

Enhanced CoderAI with comprehensive command system for better control over models, context, and sessions.

## New Interactive Chat Commands (In-Chat)

### Context Management
- **`/clear-context`** - Clear conversation context and start fresh without exiting
  - Useful for switching topics
  - Keeps session but resets message history

### Model Management
- **`/change-model`** - Switch between models/providers during chat
  - Interactive model selection
  - Supports OpenAI models and LM Studio
  - No need to restart chat session

- **`/providers`** - List available LLM providers with details
  - Shows features and requirements
  - Helps choose the right provider

### Session Management
- **`/save`** - Manually save current session
  - Force save even if auto-save is disabled
  - Get session ID confirmation

- **`/status`** - Show detailed session status
  - Session ID, model, provider
  - Message count, streaming status
  - Timestamps for created/updated

- **`/export`** - Export conversation to JSON file
  - Timestamped filename
  - Full conversation history
  - Includes metadata (model, session ID)

### Information & Diagnostics
- **`/tools`** - List all available tools
  - See what the agent can do
  - Tool descriptions included

- **`/config`** - Show current configuration
  - View all settings
  - API keys are masked for security

- **`/tokens`** - Show token usage information
  - Message count
  - Character count
  - Approximate token count

## New CLI Commands

### Model Management
- **`coderAI models`** - List all available models and providers
  - Shows OpenAI models (gpt-5, gpt-5-mini, gpt-5-nano)
  - Shows LM Studio provider
  - Displays current default

- **`coderAI set-model <model_name>`** - Set default model
  - Changes default for new sessions
  - Validates model name
  - Instant feedback

### System Diagnostics
- **`coderAI status`** - Show system status and diagnostics
  - Configuration details
  - API key status
  - LM Studio configuration
  - History statistics

## Enhanced Features

### Dynamic Prompt Display
- Prompt now shows current model: `You [gpt-5-mini]>`
- Updates automatically when model changes
- Always know which model you're using

### Improved Model Switching
- Switch models without losing conversation
- Validation of model names
- Clear feedback on model changes
- Cancel option for model change

### Better Help System
- Comprehensive `/help` with all commands
- Command descriptions
- Updated welcome message

## Technical Improvements

### Architecture
- Better separation of concerns
- Agent reference passed to interactive chat
- Context management improved
- Type hints maintained

### Error Handling
- Graceful handling of invalid model names
- Clear error messages
- Fallback to safe defaults

### User Experience
- Rich formatting for all output
- Color-coded information
- Clear success/error messages
- Intuitive command naming

## Usage Examples

### Example 1: Switching Models Mid-Conversation
```
You> /change-model
[Shows available models]
You> gpt-5
✓ Model changed from gpt-5-mini to gpt-5
You [gpt-5]> continue with complex task...
```

### Example 2: Managing Context
```
You> /status
[Shows session details]
You> /tokens
[Shows token usage]
You> /clear-context
✓ Conversation context cleared. Starting fresh!
```

### Example 3: Exporting Conversations
```
You> /export
✓ Conversation exported to: coderAI_export_abc123_20251012_143022.json
```

### Example 4: CLI Model Management
```bash
$ coderAI models
[Lists all models]

$ coderAI set-model gpt-5-mini
✓ Default model set to: gpt-5-mini

$ coderAI status
[Shows system status]
```

## Benefits

1. **Flexibility** - Switch models without restarting
2. **Control** - Clear context when needed
3. **Transparency** - View status, tokens, and configuration
4. **Productivity** - Export important conversations
5. **Convenience** - All features accessible via simple commands

## Files Modified

1. **`coderAI/ui/interactive.py`**
   - Added new command handlers
   - Enhanced context management
   - Dynamic prompt display

2. **`coderAI/cli.py`**
   - Added CLI commands (models, set-model, status)
   - Model switching logic
   - Agent reference passing

## Documentation

- **`COMMANDS.md`** - Comprehensive command reference
- **`NEW_FEATURES.md`** - This file
- Updated help text in interactive mode

## Backward Compatibility

All changes are backward compatible:
- Existing commands still work
- Old sessions can be resumed
- Configuration format unchanged
- No breaking changes

## Future Enhancements (Ideas)

- `/undo` - Undo last message
- `/retry` - Retry last response
- `/search <query>` - Search conversation history
- `/bookmark` - Bookmark important messages
- `/stats` - Detailed statistics
- `coderAI compare` - Compare different models

