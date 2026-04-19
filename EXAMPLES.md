# CoderAI Examples

This document provides examples of using CoderAI for various coding tasks.

## Getting Started

### 1. Setup

First, run the setup wizard:

```bash
coderAI setup
```

Or manually configure:

```bash
coderAI config set openai_api_key YOUR_API_KEY
coderAI config set default_model gpt-5-mini
```

### 2. Interactive Mode

Start an interactive chat session:

```bash
coderAI chat
```

Or with a specific model:

```bash
coderAI --model gpt-5 chat
```

### 3. Ask in the chat UI

There is no separate “one-shot” CLI argument: start the Ink UI, then type your question at the prompt:

```bash
coderAI chat
```

## Example Use Cases

### File Operations

**Read a file:**
```
You> Read the contents of config.py
```

**Create a new file:**
```
You> Create a Python file named hello.py with a simple hello world function
```

**Edit a file:**
```
You> In app.py, replace the old authentication logic with JWT authentication
```

### Code Analysis

**Analyze code:**
```
You> Analyze the performance bottlenecks in my Python application
```

**Find bugs:**
```
You> Search for potential security vulnerabilities in the codebase
```

**Code review:**
```
You> Review the latest changes in main.py and suggest improvements
```

### Project Tasks

**Create a web server:**
```
You> Create a Flask REST API with endpoints for user CRUD operations
```

**Add tests:**
```
You> Write pytest unit tests for the functions in utils.py
```

**Refactor code:**
```
You> Refactor the code in data_processor.py to follow better design patterns
```

### Git Operations

**Check status:**
```
You> Show me the git status and what files have changed
```

**Create commit:**
```
You> Stage all changes and create a commit with an appropriate message
```

**View history:**
```
You> Show me the last 5 commits in this repository
```

### Terminal Commands

**Run commands:**
```
You> Install the required Python packages from requirements.txt
```

**Check environment:**
```
You> Check if Node.js and npm are installed and show their versions
```

### Search Operations

**Search codebase:**
```
You> Find all files that import the requests library
```

**Pattern matching:**
```
You> Use grep to find all TODO comments in the codebase
```

### Web Search

**Find documentation:**
```
You> Search for FastAPI documentation on handling file uploads
```

**Error resolution:**
```
You> Search for solutions to the error: "ModuleNotFoundError: No module named 'pydantic'"
```

### Memory/Knowledge Base

**Save information:**
```
You> Remember that this project uses PostgreSQL database on port 5432
```

**Recall information:**
```
You> What database configuration did we save?
```

## Advanced Usage

### Resume Previous Session

List all sessions:
```bash
coderAI history list
```

Resume a specific session:
```bash
coderAI --resume session_1234567890 chat
```

### Using Local Models with LM Studio

1. Start LM Studio and load a model
2. Enable the local server (default: http://localhost:1234)
3. Run CoderAI:

```bash
coderAI --model lmstudio chat
```

### Configuration Management

Show current configuration:
```bash
coderAI config show
```

Set specific values:
```bash
coderAI config set temperature 0.8
coderAI config set max_tokens 8192
coderAI config set streaming true
```

Reset to defaults:
```bash
coderAI config reset
```

### Complex Multi-step Tasks

CoderAI can handle complex tasks that require multiple steps:

```
You> Create a complete Python package for a todo list CLI application with:
- A main.py entry point
- Functions to add, list, complete, and delete todos
- Data persistence using JSON
- Click for the CLI interface
- Unit tests
- A README with usage instructions
```

The agent will:
1. Create the directory structure
2. Write all necessary files
3. Add proper documentation
4. Create tests
5. Provide usage instructions

## Tips and Tricks

### 1. Be Specific

Instead of:
```
You> Fix my code
```

Try:
```
You> The function calculate_total in utils.py is returning incorrect values. Please review and fix it.
```

### 2. Use Context

The agent maintains conversation context:
```
You> Read the file app.py
You> Now add error handling to the database functions in that file
```

### 3. Leverage Tools

The agent will automatically use appropriate tools:
- File operations for reading/writing
- Git commands for version control
- Web search for documentation
- Memory for storing project-specific information

### 4. Iterative Refinement

```
You> Create a FastAPI application with user endpoints
You> Add authentication using JWT
You> Now add rate limiting to the API
You> Add comprehensive error handling
```

### 5. Ask for Explanations

```
You> Explain the design patterns used in this codebase
You> Why did you choose this approach for error handling?
```

## Model Selection

- **gpt-5**: Most capable, best for complex reasoning
- **gpt-5-mini**: Balanced performance and speed
- **gpt-5-nano**: Fastest, good for simple tasks
- **lmstudio**: Use your own local models

## Troubleshooting

### API Key Issues

```bash
# Set API key
coderAI config set openai_api_key sk-...

# Or use environment variable
export OPENAI_API_KEY=sk-...
```

### LM Studio Connection

```bash
# Check LM Studio is running
curl http://localhost:1234/v1/models

# Configure endpoint if different
coderAI config set lmstudio_endpoint http://localhost:1234/v1
```

### View Logs

Check `~/.coderAI/history/` for conversation logs.

## Getting Help

In interactive mode:
```
You> /help
```

Show model info:
```bash
coderAI info
```

Check version:
```bash
coderAI --version
```

