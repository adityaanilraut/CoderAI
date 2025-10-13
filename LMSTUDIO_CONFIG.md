# LM Studio Configuration Guide

This document explains how to configure CoderAI to work with your LM Studio server.

## What Changed

All hardcoded LM Studio values have been removed. You can now provide your own LM Studio server URL and model name.

### Previous Hardcoded Values (Removed)
- ❌ Hardcoded endpoint: `http://10.0.0.34:1234/v1`
- ❌ Hardcoded model: `qwen/qwen3-4b-2507`

### New Defaults (Configurable)
- ✅ Default endpoint: `http://localhost:1234/v1` (standard LM Studio default)
- ✅ Default model: `local-model` (generic name)
- ✅ Both can be customized via configuration

## Configuration Methods

### Method 1: Using CLI Commands (Recommended)

Configure your LM Studio server URL:
```bash
coderAI config set lmstudio_endpoint http://YOUR_SERVER_URL:PORT/v1
```

Optionally configure the model name:
```bash
coderAI config set lmstudio_model your-model-name
```

View your configuration:
```bash
coderAI config show
```

### Method 2: Using Setup Wizard

Run the interactive setup wizard:
```bash
coderAI setup
```

It will guide you through:
1. OpenAI API key (optional)
2. Default model selection
3. LM Studio configuration (optional)
   - Server URL
   - Model name (optional)

### Method 3: Using Environment Variables

Set environment variables before running CoderAI:
```bash
export LMSTUDIO_ENDPOINT="http://YOUR_SERVER_URL:PORT/v1"
coderAI --model lmstudio chat
```

### Method 4: Manual Configuration File

Edit `~/.coderAI/config.json`:
```json
{
  "lmstudio_endpoint": "http://YOUR_SERVER_URL:PORT/v1",
  "lmstudio_model": "your-model-name",
  "default_model": "lmstudio"
}
```

## Usage Examples

### Example 1: Using LM Studio on Localhost (Default)

If your LM Studio is running on the default port (1234), no configuration needed:
```bash
coderAI --model lmstudio chat
```

### Example 2: Using LM Studio on Custom Port

Configure custom port:
```bash
coderAI config set lmstudio_endpoint http://localhost:5000/v1
coderAI --model lmstudio chat
```

### Example 3: Using Remote LM Studio Server

Configure remote server:
```bash
coderAI config set lmstudio_endpoint http://192.168.1.100:1234/v1
coderAI --model lmstudio chat
```

### Example 4: Using Specific Model Name

If your LM Studio requires a specific model name:
```bash
coderAI config set lmstudio_model qwen/qwen3-4b-2507
coderAI --model lmstudio chat
```

## Verification

Check if your LM Studio is configured correctly:
```bash
coderAI status
```

This will show:
- Current LM Studio endpoint
- Current LM Studio model
- Other configuration details

## Common LM Studio Endpoints

| Setup | Endpoint | Notes |
|-------|----------|-------|
| Local default | `http://localhost:1234/v1` | Standard LM Studio installation |
| Local custom port | `http://localhost:PORT/v1` | If you changed the port |
| Network server | `http://IP:1234/v1` | Remote LM Studio server |
| Docker container | `http://container-name:1234/v1` | If running in Docker |

## Troubleshooting

### Connection Error
```
LM Studio API error: Cannot connect to host...
```
**Solution:** Verify your LM Studio is running and the endpoint is correct:
```bash
# Check your configured endpoint
coderAI config show

# Test connection (example)
curl http://YOUR_ENDPOINT/models
```

### Wrong Model Name
If LM Studio returns an error about the model not being found, configure the correct model name:
```bash
coderAI config set lmstudio_model correct-model-name
```

### Reset to Defaults
To reset LM Studio configuration to defaults:
```bash
coderAI config set lmstudio_endpoint http://localhost:1234/v1
coderAI config set lmstudio_model local-model
```

Or reset all configuration:
```bash
coderAI config reset
```

## Technical Details

### Files Modified

1. **coderAI/llm/lmstudio.py**
   - Removed hardcoded endpoint: `http://10.0.0.34:1234/v1`
   - Removed hardcoded model: `qwen/qwen3-4b-2507`
   - New defaults: `http://localhost:1234/v1` and `local-model`

2. **coderAI/config.py**
   - Updated default `lmstudio_endpoint` to `http://localhost:1234/v1`
   - Updated default `lmstudio_model` to `local-model`

3. **coderAI/cli.py**
   - Enhanced setup wizard to ask for both endpoint and model name
   - Improved user prompts with better descriptions

4. **README.md**
   - Updated configuration examples
   - Added detailed LM Studio setup instructions

### Configuration Priority

Configuration values are loaded in this order (highest priority first):
1. Environment variables (`LMSTUDIO_ENDPOINT`)
2. Configuration file (`~/.coderAI/config.json`)
3. Default values (built-in)

## Support

For issues or questions:
1. Check your LM Studio is running: Open LM Studio and ensure a model is loaded
2. Verify the endpoint: Look at the LM Studio interface for the server URL
3. Test the connection: Use `curl` or a browser to test the endpoint
4. Check configuration: Run `coderAI config show` to see current settings
5. View system status: Run `coderAI status` for diagnostics

---

**Note:** LM Studio must be running with a model loaded before using it with CoderAI.

