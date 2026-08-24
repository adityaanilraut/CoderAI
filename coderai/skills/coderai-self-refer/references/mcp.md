# CoderAI MCP Configuration

Configure MCP servers in `~/.coderai/settings.json` or `<project>/.coderai/settings.json`. Project entries merge over user entries by server name.

## Stdio server

```json
{
  "mcpServers": {
    "local-tools": {
      "command": "python3",
      "args": ["-m", "my_mcp_server"],
      "cwd": "/absolute/path/to/server",
      "env": {
        "SERVICE_TOKEN": "set-locally"
      }
    }
  }
}
```

`command` is executed directly with `args`; CoderAI does not add package-manager flags. On Windows, command lines are launched through the platform shell with argument quoting.

## Remote SSE server

```json
{
  "mcpServers": {
    "remote": {
      "url": "https://mcp.example.com/sse",
      "headers": {
        "Authorization": "Bearer set-locally"
      }
    }
  }
}
```

Supported server fields:

| Field | Meaning |
| --- | --- |
| `command` | Stdio executable; use either `command` or `url` |
| `args` | Stdio argument list |
| `cwd` | Stdio working directory |
| `env` | Environment added to the stdio child process |
| `url` | Remote SSE endpoint |
| `headers` | HTTP headers for a remote endpoint |
| `enabled` | Set to `false` to skip the server |
| `disabled` | Set to `true` to skip the server |
| `allowPrivateIps` | Permit remote URLs resolving to private or loopback addresses |

Keep tokens out of committed project settings. Prefer environment expansion provided by the server command, a protected user-level settings file, or another local secret mechanism supported by that server.

## Tool names

Discovered tools use `mcp__<server>__<tool>`. CoderAI sanitizes names to API-safe characters and may shorten long names, so `/mcp` is the authoritative list.

## Commands

- `/mcp` shows servers and tools.
- `/mcp reconnect` reconnects configured servers.
- `/mcp prompts` lists MCP prompts.
- `/mcp resources` lists resources.

## Troubleshooting

1. Run `/mcp` and inspect the server error.
2. Confirm the executable exists or the remote URL is reachable.
3. Check `args`, `cwd`, required environment variables, and headers.
4. Confirm the `mcp` permission scope is allowed or approved.
5. Use `allowPrivateIps` only when a trusted local/private endpoint is intentional.
