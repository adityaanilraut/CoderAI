# Installation Guide for CoderAI

This guide will help you install and set up CoderAI on your system.

## Prerequisites

- Python 3.10 or higher
- pip (Python package installer)
- Git (optional, for version control features)

## How `coderAI` is packaged

CoderAI ships as a single pure-Python wheel on PyPI. The agent, tools,
one-shot CLI commands (`status`, `setup`, `history`, `models`, `config`,
`info`, `cost`, `doctor`, `index`, `search`, `run`, `mcp`, `tasks`), and
the interactive Textual TUI all live inside the wheel. There are no native
binaries or extra download steps — `pip install coderai-agent` is the whole story.

## Installation Methods

### Method 1: Install from PyPI (recommended for most users)

```bash
pip install coderai-agent
coderAI chat
```

This installs the agent and Textual TUI in one step. `coderAI chat`
launches the interactive UI directly from Python.

### Method 2: Install from source (for contributors)

```bash
git clone https://github.com/adityaanilraut/CoderAI.git
cd CoderAI
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -e ".[dev]"
```

`pip install -e ".[dev]"` adds pytest/ruff/mypy (and related stubs) for the
test and lint targets. `make dev` is a shortcut for the same install.

### Optional extras

```bash
# Semantic code search (ChromaDB) — enables `coderAI index` / `search`
pip install -e ".[semantic]"

# Optional private, on-device embeddings (combine with semantic search)
pip install -e ".[semantic,local-embeddings]"

# Browser automation (Playwright)
pip install -e ".[browser]"
playwright install chromium

# PDF extraction in read_url
pip install -e ".[web]"
```

Embedding selection defaults to `auto`: OpenAI is used when `OPENAI_API_KEY`
is set, otherwise the local backend is selected. To require local inference:

```bash
coderAI config set embedding_backend local
# Optional model/device overrides:
coderAI config set embedding_model sentence-transformers/all-MiniLM-L6-v2
coderAI config set embedding_device cpu
```

The first local run may download the configured model. Later inference runs on
the selected device and does not send source chunks to an embeddings API.

## Verify Installation

Check that CoderAI is installed correctly:

```bash
coderAI --version
```

You should see: `CoderAI version 0.3.0`

## Initial Setup

### Quick Setup (Interactive)

Run the setup wizard:

```bash
coderAI setup
```

This lets you choose a provider, configure the credentials or local endpoint it
needs, select a default model, and set reasoning preferences.

### Manual Setup

1. **Set your OpenAI API key:**

   ```bash
   coderAI config set openai_api_key YOUR_API_KEY
   ```

   Or use an environment variable:

   ```bash
   export OPENAI_API_KEY="your-api-key-here"
   ```

2. **Set default model (optional):**

   ```bash
   coderAI config set default_model gpt-5.4-mini
   ```

3. **Configure LM Studio for local models (optional):**
   ```bash
   coderAI config set lmstudio_endpoint http://localhost:1234/v1
   ```

## Configuration Files

CoderAI stores configuration and history in `~/.coderAI/`:

```
~/.coderAI/
├── config.json           # Configuration settings
├── history/              # Conversation history
│   └── session_*.json
├── memory/               # Persistent memory store
│   └── memories.json
└── index/                # Semantic code search index
    ├── manifest.json     #   File-hash index manifest
    └── vectordb/         #   ChromaDB persistent store
```

## Getting Your OpenAI API Key

1. Go to https://platform.openai.com/
2. Sign in or create an account
3. Navigate to API Keys section
4. Create a new API key
5. Copy and save it securely

**Note:** Keep your API key secret and never commit it to version control.

## Using Local Models with LM Studio

You can run CoderAI with local LLMs using [LM Studio](https://lmstudio.ai/).

### 1. Prerequisites

1.  **Download and Install LM Studio**: Visit https://lmstudio.ai/
2.  **Load a Model**: In LM Studio, search for and download a model (e.g., Llama 3, Mistral, Qwen).
3.  **Start Local Server**: In LM Studio, go to the "Local Server" tab and click "Start Server".
    - Default endpoint: `http://localhost:1234/v1`

### 2. Configuration Methods

#### Method A: Using CLI (Recommended)

Configure the server URL:

```bash
coderAI config set lmstudio_endpoint http://localhost:1234/v1
```

Optionally, set a specific model name (default is `local-model`):

```bash
coderAI config set lmstudio_model your-model-name
```

#### Method B: Interactive Setup

Run the wizard and follow the prompts:

```bash
coderAI setup
```

#### Method C: Environment Variables

```bash
export LMSTUDIO_ENDPOINT="http://localhost:1234/v1"
coderAI chat --model lmstudio
```

### 3. Verification

Check your configuration:

```bash
coderAI status
```

### 4. Usage

Start a chat session with the local model:

```bash
coderAI chat --model lmstudio
```

## Troubleshooting

### Connection Errors

If you see "Cannot connect to host", verify:

1.  LM Studio server is running (Green indicator in LM Studio).
2.  The endpoint URL is correct in `coderAI config show`.
3.  You can reach the server: `curl http://localhost:1234/v1/models`

### Model Errors

If LM Studio complains about the model name, set it to match the loaded model:

```bash
coderAI config set lmstudio_model expected-model-name
```

## Testing Your Installation

### 1. Test Basic Functionality

```bash
coderAI info
```

This should display information about CoderAI, the current model, and available tools.

### 2. Test Single-shot Mode

```bash
coderAI run "What is Python?"
```

### 3. Test Interactive Mode

```bash
coderAI chat
```

Try some commands:

- `Hello, can you help me code?`
- `/help` - Show help
- `/exit` - Exit

## Troubleshooting

### Command Not Found

If you get `coderAI: command not found`:

1. Make sure you're in the virtual environment:

   ```bash
   source venv/bin/activate
   ```

2. Reinstall:

   ```bash
   pip install -e .
   ```

3. Check if the script is in your PATH:
   ```bash
   which coderAI
   ```

### Import Errors

If you see import errors, reinstall from the package metadata (not
`requirements.txt` — that file is only a `-e .` shim):

```bash
pip install -e ".[dev]" --upgrade
```

For a fully pinned, hashed install matching CI audits:

```bash
pip install -r requirements.lock
# or regenerate: make lock
```

### API Key Issues

If you get authentication errors:

1. Verify your API key:

   ```bash
   coderAI config show
   ```

2. Set it again:

   ```bash
   coderAI config set openai_api_key YOUR_KEY
   ```

3. Or use environment variable:
   ```bash
   export OPENAI_API_KEY="your-key"
   coderAI chat
   ```

### LM Studio Connection Issues

If LM Studio isn't working:

1. Verify LM Studio is running:

   ```bash
   curl http://localhost:1234/v1/models
   ```

2. Check the endpoint:

   ```bash
   coderAI config show
   ```

3. Update if needed:
   ```bash
   coderAI config set lmstudio_endpoint http://localhost:1234/v1
   ```

## Updating CoderAI

If you installed from source:

```bash
cd CoderAI
git pull
pip install -e . --upgrade
```

## Uninstalling

To uninstall CoderAI:

```bash
pip uninstall coderai-agent
```

To remove configuration and history:

```bash
rm -rf ~/.coderAI
```

## Environment Variables

See [`.env.example`](../.env.example) for the full list of provider keys and
`CODERAI_*` flags. Common ones:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | Provider credentials |
| `CODERAI_DEFAULT_MODEL` | Default model / alias |
| `CODERAI_TEMPERATURE` | Sampling temperature (0.0–2.0) |
| `CODERAI_MAX_TOKENS` | Max generation tokens |
| `CODERAI_REASONING_EFFORT` | `high` / `medium` / `low` / `none` |
| `CODERAI_BUDGET_LIMIT` | Max USD per session (`0` = unlimited) |
| `CODERAI_MAX_ITERATIONS` | Max agentic loop iterations per message |
| `CODERAI_LOG_LEVEL` | Python log level (default `WARNING`) |
| `CODERAI_SANDBOX_MODE` | `off` (default), `best_effort`, or fail-closed `required` OS execution sandbox |
| `CODERAI_SANDBOX_ALLOW_NETWORK` | `1` = permit network from actively sandboxed subprocesses |
| `LMSTUDIO_ENDPOINT` / `OLLAMA_ENDPOINT` | Local inference endpoints |
| `CODERAI_TRUST_WORKSPACE` | `1` = trust every workspace (escape hatch) |
| `CODERAI_ALLOW_OUTSIDE_PROJECT` | `1` = allow FS tools outside project root |
| `CODERAI_ALLOW_LOCAL_URLS` | `1` = allow web tools to hit localhost |

Example:

```bash
cp .env.example .env   # then edit; or export in your shell
export OPENAI_API_KEY="sk-..."
export CODERAI_DEFAULT_MODEL="gpt-5.4-mini"
coderAI chat
```

For OS confinement, install the `bubblewrap` package on Linux and verify that
unprivileged user namespaces are enabled. macOS normally ships
`/usr/bin/sandbox-exec`; no extra package is needed. Confirm availability by
setting `CODERAI_SANDBOX_MODE=required` and running a command. Windows has no OS
sandbox backend.

**Platforms:** Linux and macOS are fully supported. Windows is best-effort
(see [SECURITY.md](../SECURITY.md#supported-platforms)).

## Next Steps

After installation:

1. Read the [README.md](../README.md) for feature overview
2. Check [EXAMPLES.md](EXAMPLES.md) for usage examples
3. Run `coderAI setup` for interactive configuration
4. Start coding with `coderAI chat`

## Getting Help

- Run `coderAI --help` for command help
- Run `coderAI info` for system information
- Check the documentation in the repository

## Support

For issues or questions:

- Check the troubleshooting section above
- Review the examples in EXAMPLES.md
- Check system info: `coderAI info`
