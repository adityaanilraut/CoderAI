# Installation Guide for CoderAI

This guide will help you install and set up CoderAI on your system.

## Prerequisites

- Python 3.9 or higher
- pip (Python package installer)
- Git (optional, for version control features)

## Installation Methods

### Method 1: Install from Source (Recommended for Development)

1. **Clone or navigate to the repository:**

   ```bash
   git clone https://github.com/your-username/coderAI.git
   cd coderAI
   ```

2. **Create a virtual environment (recommended):**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install in development mode:**

   ```bash
   pip install -e .
   ```

   This installs the package in "editable" mode, allowing you to make changes to the code.

### Method 2: Install from Requirements File

```bash
pip install -r requirements.txt
pip install .
```

### Method 3: Direct Dependency Installation

```bash
pip install rich click openai requests pydantic aiohttp tiktoken python-dotenv prompt-toolkit
```

Then install the package:

```bash
pip install -e .
```

## Verify Installation

Check that CoderAI is installed correctly:

```bash
coderAI --version
```

You should see: `CoderAI version 0.1.0`

## Initial Setup

### Quick Setup (Interactive)

Run the setup wizard:

```bash
coderAI setup
```

This will guide you through:

1. Setting your OpenAI API key
2. Choosing a default model
3. Configuring LM Studio (optional)

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
   coderAI config set default_model gpt-5-mini
   ```

3. **Configure LM Studio for local models (optional):**
   ```bash
   coderAI config set lmstudio_endpoint http://localhost:1234/v1
   ```

## Configuration Files

CoderAI stores configuration and history in `~/.coderAI/`:

```
~/.coderAI/
├── config.json          # Configuration settings
├── history/             # Conversation history
│   └── session_*.json
└── memory/              # Knowledge base
    └── memories.json
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
coderAI --model lmstudio chat
```

### 3. Verification

Check your configuration:

```bash
coderAI status
```

### 4. Usage

Start a chat session with the local model:

```bash
coderAI --model lmstudio chat
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
coderAI "What is Python?"
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

If you see import errors:

```bash
pip install --upgrade -r requirements.txt
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
cd coderAI
git pull
pip install -e . --upgrade
```

## Uninstalling

To uninstall CoderAI:

```bash
pip uninstall coderAI
```

To remove configuration and history:

```bash
rm -rf ~/.coderAI
```

## Environment Variables

CoderAI supports these environment variables:

- `OPENAI_API_KEY` - OpenAI API key
- `LMSTUDIO_ENDPOINT` - LM Studio API endpoint
- `CODERAI_DEFAULT_MODEL` - Default model to use
- `CODERAI_TEMPERATURE` - Temperature for generation (0.0-2.0)
- `CODERAI_MAX_TOKENS` - Maximum tokens to generate

Example:

```bash
export OPENAI_API_KEY="sk-..."
export CODERAI_DEFAULT_MODEL="gpt-5-mini"
export CODERAI_TEMPERATURE="0.7"
coderAI chat
```

## Next Steps

After installation:

1. Read the [README.md](README.md) for feature overview
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
