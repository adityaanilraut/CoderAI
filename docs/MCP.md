# Model Context Protocol (MCP) in CoderAI

CoderAI includes a native, full-featured **Model Context Protocol (MCP)** client supporting tools, prompts, and resources across multiple server transports.

---

## Supported Transports

1. **`stdio`**: Communicates with local processes over standard input/output.
2. **`sse`**: Connects to remote or local HTTP servers using Server-Sent Events.
3. **`streamable-http`**: Streaming HTTP transport with bidirectional message streams.

---

## Configuring MCP Servers

MCP servers are defined in `.coderai/settings.json` (project-scoped) or `~/.coderai/settings.json` (user-scoped):

```json
{
  "mcpServers": {
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./app.db"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    },
    "remote-api": {
      "url": "https://mcp.internal.company.com/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer ${MCP_REMOTE_TOKEN}"
      },
      "allowPrivateIps": false
    },
    "disabled-server": {
      "command": "echo",
      "disabled": true
    }
  }
}
```

### Configuration Options

| Option | Type | Description |
|---|---|---|
| `command` | string | Executable name or path for stdio servers |
| `args` | array | Command-line arguments for stdio servers |
| `url` | string | Endpoint URL for SSE / HTTP servers |
| `transport` | string | `stdio`, `sse`, or `streamable-http` |
| `env` | object | Environment variables passed to the subprocess (supports `${VAR}` expansion) |
| `headers` | object | HTTP headers for remote SSE/HTTP servers |
| `disabled` | boolean | Set `true` to disable without removing configuration |
| `allowPrivateIps`| boolean | Set `true` to allow private/loopback network addresses |

---

## Interactive MCP Commands

In the interactive CLI, use `/mcp` to manage and inspect connected servers:

- **`/mcp`**: Opens the interactive MCP inspector showing server status, tools, prompts, and resources.
- **`/mcp list`**: Displays all configured servers and connection status.
- **`/mcp reconnect`**: Re-initializes all connected servers.
