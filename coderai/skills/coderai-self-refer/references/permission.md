# CoderAI Permissions

CoderAI maps tool calls to permission scopes before execution.

| Scope | Operation |
| --- | --- |
| `read-in-cwd` | Read inside the workspace |
| `read-out-cwd` | Read outside the workspace |
| `write-in-cwd` | Write inside the workspace |
| `write-out-cwd` | Write outside the workspace |
| `delete-in-cwd` | Delete inside the workspace |
| `delete-out-cwd` | Delete outside the workspace |
| `query-git-log` | Read Git history |
| `mutate-git-log` | Change Git history |
| `network` | Access the network |
| `mcp` | Call MCP tools |

`unknown` is an internal shell-classification fallback. It always prompts and is not a configurable scope.

## Presets

Use `/permission` to view the current mode or set one of:

- `read-only`
- `workspace-write`
- `danger-full-access`

The CLI also accepts specialized read presets shown by `/help permission`. Prefer the three primary presets unless the user needs a narrower read policy.

Persist a preset in settings:

```json
{
  "permissions": {
    "preset": "workspace-write"
  }
}
```

## Explicit rules

```json
{
  "permissions": {
    "allow": ["read-in-cwd", "write-in-cwd", "query-git-log"],
    "deny": ["write-out-cwd", "delete-out-cwd"],
    "ask": ["network", "mcp", "mutate-git-log"],
    "defaultMode": "askAll"
  }
}
```

`defaultMode` is `allowAll` or `askAll`. Evaluation order is:

1. Any matching `deny` rejects the call.
2. Otherwise, any matching `ask` prompts.
3. Otherwise, all scopes explicitly in `allow` are allowed.
4. Remaining scopes use `defaultMode`.

User and project rule lists are combined; project settings do not erase user lists. A project `defaultMode` or preset takes precedence when present.

Choosing the persistent allow option in a prompt writes the scope to `<project>/.coderai/settings.json`. Review that file before sharing it.
