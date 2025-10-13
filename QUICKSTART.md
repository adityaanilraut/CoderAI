# CoderAI Quick Start Guide

Get up and running with CoderAI in 5 minutes!

## Prerequisites

- Python 3.9 or higher
- pip
- OpenAI API key (or LM Studio for local models)

## Step 1: Install (30 seconds)

```bash
cd /Users/aditya/Desktop/vibe/coderAI
pip install -e .
```

## Step 2: Setup (2 minutes)

### Option A: Interactive Setup (Recommended)

```bash
coderAI setup
```

Follow the prompts to enter:
1. OpenAI API key
2. Default model (press Enter for gpt-5-mini)
3. LM Studio config (optional, press N to skip)

### Option B: Manual Setup

```bash
coderAI config set openai_api_key YOUR_API_KEY
coderAI config set default_model gpt-5-mini
```

Or use environment variable:
```bash
export OPENAI_API_KEY="sk-your-key-here"
```

## Step 3: Verify (10 seconds)

```bash
coderAI --version
coderAI info
```

## Step 4: Start Using! (Now!)

### Interactive Mode

```bash
coderAI chat
```

Try these in the chat:
```
You> Create a Python hello world script
You> Read the file you just created
You> Add a function to calculate fibonacci numbers
You> /help
You> /exit
```

### Single-shot Mode

```bash
coderAI "What files are in the current directory?"
coderAI "Create a README.md with installation instructions"
coderAI "Show me the git status"
```

## Common First Tasks

**Create a web application:**
```bash
coderAI chat
You> Create a Flask REST API with user CRUD endpoints
```

**Analyze existing code:**
```bash
coderAI "Analyze app.py and suggest improvements"
```

**Fix a bug:**
```bash
coderAI "The login function in auth.py is failing, please fix it"
```

**Write tests:**
```bash
coderAI "Write pytest unit tests for utils.py"
```

## Using Local Models (LM Studio)

1. Download and install [LM Studio](https://lmstudio.ai/)
2. Load a model in LM Studio
3. Start the local server (default: http://localhost:1234)
4. Run:
   ```bash
   coderAI --model lmstudio chat
   ```

## Available Commands

```bash
coderAI chat                 # Interactive mode
coderAI "prompt"             # Single-shot
coderAI --model MODEL chat   # Use specific model
coderAI --resume ID          # Resume session
coderAI config show          # View configuration
coderAI history list         # View past sessions
coderAI info                 # System information
coderAI --help               # Full help
```

## Available Models

- `gpt-5` - Most capable (uses GPT-4 until GPT-5 release)
- `gpt-5-mini` - Balanced (default, uses GPT-4 Turbo)
- `gpt-5-nano` - Fastest (uses GPT-3.5 Turbo)
- `lmstudio` - Your local model

## Interactive Mode Commands

```
/help      - Show help
/clear     - Clear screen
/history   - Show conversation
/model     - Model information
/exit      - Exit
```

## What Can CoderAI Do?

✅ Read, write, and edit files  
✅ Execute terminal commands  
✅ Git operations (status, diff, commit, log)  
✅ Search your codebase  
✅ Search the web for documentation  
✅ Remember information across sessions  
✅ Create complete applications  
✅ Debug and fix code  
✅ Write tests  
✅ And much more!  

## Troubleshooting

**Command not found?**
```bash
pip install -e .
```

**API key error?**
```bash
coderAI config set openai_api_key YOUR_KEY
```

**LM Studio not connecting?**
```bash
# Verify LM Studio server is running
curl http://localhost:1234/v1/models

# Set endpoint if different
coderAI config set lmstudio_endpoint http://localhost:1234/v1
```

**Test installation:**
```bash
python test_installation.py
```

## Next Steps

📖 Read [EXAMPLES.md](EXAMPLES.md) for more usage examples  
🏗️ Read [ARCHITECTURE.md](ARCHITECTURE.md) to understand internals  
📦 Read [INSTALL.md](INSTALL.md) for detailed installation  
📋 Read [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) for complete overview  

## Useful Makefile Commands

```bash
make help        # Show all commands
make dev         # Install in dev mode
make test        # Run tests
make run         # Start interactive mode
make setup       # Run setup wizard
make clean       # Clean build artifacts
```

## Tips for Best Results

1. **Be specific**: "Fix the bug in line 45 of app.py" vs "fix bug"
2. **Use context**: The agent remembers conversation history
3. **Iterate**: Build complex features step by step
4. **Ask questions**: "Explain why this code fails"
5. **Use tools**: The agent will automatically use appropriate tools

## Example Session

```bash
$ coderAI chat

CoderAI> Create a todo list CLI app in Python

[Agent creates files: main.py, todo.py, README.md]

CoderAI> Add a feature to mark todos as complete

[Agent modifies todo.py]

CoderAI> Write tests for this

[Agent creates test_todo.py]

CoderAI> Run the tests

[Agent executes: pytest test_todo.py]

CoderAI> Create a git commit with these changes

[Agent runs: git add . && git commit -m "..."]

CoderAI> /exit
```

## That's It! 🚀

You're ready to use CoderAI. Start with:

```bash
coderAI chat
```

Happy coding! 🎉

