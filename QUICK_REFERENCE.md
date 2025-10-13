# CoderAI Quick Reference Card

## 🚀 Getting Started

```bash
coderAI setup              # First-time setup
coderAI chat               # Start chatting
coderAI status             # Check system status
```

---

## 💬 Interactive Commands (In Chat)

### Essential Commands
| Command | Description | Example |
|---------|-------------|---------|
| `/help` | Show help | `/help` |
| `/exit` | Exit chat | `/exit` |
| `/clear` | Clear screen | `/clear` |

### Model Management
| Command | Description | Example |
|---------|-------------|---------|
| `/change-model` | Switch model | `/change-model` → `gpt-5` |
| `/model` | Show model info | `/model` |
| `/providers` | List providers | `/providers` |

### Session Control
| Command | Description | Example |
|---------|-------------|---------|
| `/clear-context` | Start fresh | `/clear-context` |
| `/save` | Save session | `/save` |
| `/export` | Export to JSON | `/export` |
| `/status` | Session status | `/status` |

### Information
| Command | Description | Example |
|---------|-------------|---------|
| `/history` | Show messages | `/history` |
| `/tokens` | Token usage | `/tokens` |
| `/tools` | List tools | `/tools` |
| `/config` | Show config | `/config` |

---

## 🖥️  CLI Commands

### Basic Operations
```bash
coderAI chat                        # Start interactive chat
coderAI "your question"             # Quick question
coderAI ask "your question"         # Single-shot mode
```

### Model Management
```bash
coderAI models                      # List available models
coderAI set-model gpt-5-mini        # Set default model
coderAI chat -m gpt-5               # Use specific model
```

### Configuration
```bash
coderAI config show                 # View configuration
coderAI config set KEY VALUE        # Set value
coderAI config reset                # Reset to defaults
```

### Session Management
```bash
coderAI chat -r SESSION_ID          # Resume session
coderAI history list                # List sessions
coderAI history delete ID           # Delete session
coderAI history clear               # Clear all
```

### System
```bash
coderAI status                      # System status
coderAI info                        # Agent info
coderAI setup                       # Setup wizard
coderAI --version                   # Show version
```

---

## 📋 Common Workflows

### Workflow 1: Simple Chat
```bash
coderAI chat
You> Hello!
Assistant> Hi! How can I help?
You> /exit
```

### Workflow 2: Switch Models
```bash
coderAI chat -m gpt-5-nano
You> Start with quick task...
You> /change-model
Available Models: ...
You> gpt-5
✓ Model changed
You [gpt-5]> Now do complex task...
```

### Workflow 3: Manage Context
```bash
You> Long conversation...
You> /tokens
Approx tokens: 3500
You> /export
✓ Exported to file
You> /clear-context
✓ Context cleared
You> Start new topic...
```

### Workflow 4: Configuration
```bash
# Check status
coderAI status

# Set API key
coderAI config set openai_api_key sk-...

# Change default model
coderAI set-model gpt-5-mini

# Verify
coderAI config show
```

---

## 🎯 Model Selection Guide

| Model | Use For | Speed | Cost |
|-------|---------|-------|------|
| `gpt-5` | Complex tasks, reasoning | Slow | High |
| `gpt-5-mini` | Most tasks (balanced) | Medium | Medium |
| `gpt-5-nano` | Quick tasks, iterations | Fast | Low |
| `lmstudio` | Local, private tasks | Varies | Free |

**Recommendation**: Start with `gpt-5-mini`, switch to `gpt-5` when needed.

---

## ⚙️  Configuration Keys

| Key | Type | Description | Example |
|-----|------|-------------|---------|
| `openai_api_key` | string | OpenAI API key | `sk-...` |
| `default_model` | string | Default model | `gpt-5-mini` |
| `temperature` | float | Creativity (0-2) | `0.7` |
| `max_tokens` | int | Max response length | `4096` |
| `streaming` | bool | Enable streaming | `true` |
| `save_history` | bool | Save sessions | `true` |
| `lmstudio_endpoint` | string | LM Studio URL | `http://...` |

---

## 🔑 Environment Variables

```bash
export OPENAI_API_KEY="sk-..."
export CODERAI_DEFAULT_MODEL="gpt-5-mini"
export CODERAI_TEMPERATURE="0.7"
export LMSTUDIO_ENDPOINT="http://localhost:1234/v1"
```

---

## 💡 Tips & Tricks

### Tip 1: Quick Context Clear
```bash
You> /clear-context    # Instead of restarting
```

### Tip 2: Monitor Tokens
```bash
You> /tokens           # Check periodically
You> /export           # Save before clearing
You> /clear-context    # Clear when too large
```

### Tip 3: Model for Task
```bash
# Quick iterations
You> /change-model → gpt-5-nano

# Complex reasoning
You> /change-model → gpt-5
```

### Tip 4: Save Important Sessions
```bash
You> Great conversation!
You> /save
You> /export
```

### Tip 5: System Check
```bash
coderAI status         # Before starting
```

---

## 🐛 Troubleshooting

### Problem: API Key Error
```bash
# Solution
coderAI config set openai_api_key YOUR_KEY
```

### Problem: Model Not Found
```bash
# Check available models
coderAI models

# Set valid model
coderAI set-model gpt-5-mini
```

### Problem: LM Studio Not Working
```bash
# Check status
coderAI status

# Configure endpoint
coderAI config set lmstudio_endpoint http://localhost:1234/v1
```

### Problem: Large Context
```bash
You> /tokens           # Check size
You> /export           # Save first
You> /clear-context    # Clear
```

---

## 📊 Status Indicators

| Symbol | Meaning |
|--------|---------|
| ✓ | Success |
| ✗ | Error |
| ℹ | Information |
| ⚠ | Warning |

---

## 🔍 Quick Help

```bash
# In terminal
coderAI --help

# In chat
/help

# Documentation
cat COMMANDS.md           # Full reference
cat README.md             # Overview
cat NEW_FEATURES.md       # New features
```

---

## 📞 Quick Support

1. **Command not found?** → Check `/help`
2. **Model issues?** → Run `coderAI models`
3. **Configuration?** → Run `coderAI status`
4. **Documentation?** → Read `COMMANDS.md`

---

## ⌨️  Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+C` | Interrupt (not exit) |
| `Ctrl+D` | Exit chat |
| `Up/Down` | Navigate history |

---

## 🎨 Output Colors

- **Cyan**: Headers and titles
- **Yellow**: Important values (models, IDs)
- **Green**: Success messages
- **Red**: Error messages
- **Blue**: Information
- **Dim**: Less important info

---

**Remember**: Type `/help` anytime for assistance!

**Pro Tip**: Start with `coderAI status` to verify your setup!

