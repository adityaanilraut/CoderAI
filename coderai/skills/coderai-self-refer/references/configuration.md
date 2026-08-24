# CoderAI Configuration

## Files and precedence

CoderAI reads:

1. `~/.coderai/settings.json`
2. `<project>/.coderai/settings.json`
3. `CODERAI_*` process environment variables

Later sources win for scalar settings. CoderAI also loads `~/.coderai/.env` and `<project>/.env` without replacing variables already present in the process environment. Use `/config` to inspect the resolved configuration.

Use canonical camelCase keys in JSON:

```json
{
  "model": "provider-model-name",
  "baseURL": "https://api.example.com/v1",
  "apiKey": "replace-locally",
  "thinkingEnabled": true,
  "reasoningEffort": "high"
}
```

Prefer `CODERAI_API_KEY` or `OPENAI_API_KEY` over a committed project file:

```bash
export CODERAI_API_KEY="..."
coderai
```

## Supported top-level settings

| Key | Type | Purpose |
| --- | --- | --- |
| `model` | string | Active model name |
| `baseURL` | string | OpenAI-compatible API base URL |
| `apiKey` | string | API credential |
| `contextWindow` | number or `K`/`M` string | Context token limit |
| `autoCompactWindow` | number or `K`/`M` string | Compaction threshold, capped at the context window |
| `temperature` | number from 0 to 2 | Sampling temperature |
| `thinkingEnabled` | boolean | Enable model reasoning where supported |
| `reasoningEffort` | string | `off`, `low`, `medium`, `high`, or `max` |
| `debugLogEnabled` | boolean | Enable debug logging |
| `multimodal` | string | `on`, `off`, or `default` |
| `notify` | string | Notification script path or command |
| `webSearchTool` | string | Custom web-search executable |
| `mcpServers` | object | MCP server definitions |
| `permissions` | object | Permission rules or preset |
| `enabledSkills` | object | Skill-name-to-boolean map |
| `skillScanPaths` | string array | Additional skill discovery roots |
| `statusline` | object | Status-line providers and display settings |
| `env` | string map | Environment passed to helper processes |

The `env` object is not a substitute for top-level model settings. For example, use `apiKey` in JSON or `CODERAI_API_KEY` in the process environment, not `env.API_KEY`.

## Environment overrides

The main overrides are:

```text
CODERAI_MODEL
CODERAI_BASE_URL
CODERAI_API_KEY
CODERAI_CONTEXT_WINDOW
CODERAI_AUTO_COMPACT_WINDOW
CODERAI_TEMPERATURE
CODERAI_THINKING_ENABLED
CODERAI_REASONING_EFFORT
CODERAI_DEBUG_LOG_ENABLED
CODERAI_MULTIMODAL
CODERAI_NOTIFY
CODERAI_WEB_SEARCH_TOOL
CODERAI_PERMISSION_PRESET
```

`OPENAI_API_KEY` and `OPENAI_BASE_URL` are compatibility fallbacks when the corresponding CoderAI values are absent.

## Skills

```json
{
  "enabledSkills": {
    "image-generator": false
  },
  "skillScanPaths": ["/absolute/path/to/extra-skills"]
}
```

Project `enabledSkills` entries override user entries with the same skill name. Scan paths from both files are combined.

## Related references

- MCP: `mcp.md`
- Permissions: `permission.md`
- Notifications: `notify.md`
