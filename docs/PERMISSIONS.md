# CoderAI Permissions & Security

CoderAI enforces a **defense-in-depth, fail-closed** security architecture designed to prevent unintended, unauthorized, or destructive operations on your system.

---

## 1. Permission Scopes

Every tool action declares one or more side-effect scopes before execution:

| Scope | Description | Default Policy |
|---|---|---|
| `read-in-cwd` | Reading files within the workspace root | `allow` |
| `read-out-cwd` | Reading files outside the workspace root | `ask` |
| `write-in-cwd` | Creating or modifying files within workspace | `ask` |
| `write-out-cwd` | Creating or modifying files outside workspace | `ask` |
| `delete-in-cwd` | Deleting files within workspace | `ask` |
| `delete-out-cwd` | Deleting files outside workspace | `deny` |
| `query-git-log` | Inspecting git status, diffs, or commit log | `allow` |
| `mutate-git-log` | Committing, branching, resetting, or rebasing git | `ask` |
| `network` | Outbound HTTP requests and web searches | `ask` |
| `mcp` | Calling external MCP server tools | `ask` |

---

## 2. Permission Presets

CoderAI provides 3 convenient presets to quickly configure security policy:

| Preset | Description | Sandbox Policy |
|---|---|---|
| **`read-only`** | Only read operations are permitted. All writes and mutative commands are blocked or require confirmation. | Active OS Sandbox |
| **`workspace-write`** | Read and write operations within the current working directory are allowed. Out-of-workspace writes and dangerous operations require confirmation. | Active OS Sandbox |
| **`danger-full-access`** | Unrestricted access across the system (default for developer workflows with user confirmation). | Unconfined |

Configure the preset via CLI slash command:
```bash
/permission read-only
/permission workspace-write
/permission danger-full-access
```

Or in `settings.json`:
```json
{
  "permissions": {
    "preset": "workspace-write"
  }
}
```

---

## 3. OS Sandboxing

When operating under `read-only` or `workspace-write` presets, shell commands are executed inside an OS-level sandbox:

- **macOS**: Utilizes `sandbox-exec` with a compiled Seatbelt profile restricting file writes outside the workspace and blocking unauthorized IPC.
- **Linux**: Utilizes `bwrap` (Bubblewrap) to create isolated namespaces with read-only root filesystems and writeable workspace mounts.
- **Fail-Safe Fallback**: If sandboxing utilities are unavailable on the host, CoderAI falls back to strict user approval (`ask`).

---

## 4. PreToolUse Hooks

PreToolUse hooks allow organizations and developers to execute arbitrary validation scripts before any tool runs.

Configure hooks in `.coderai/hooks.json`:
```json
{
  "preToolUse": [
    {
      "command": "python scripts/security_check.py --tool {tool_name} --args '{tool_args}'",
      "failAction": "deny"
    }
  ]
}
```

If the hook script returns a non-zero exit code, the tool call is denied immediately.

---

## 5. Network Security & SSRF Protection

Outbound HTTP requests via `WebFetch` and web search tools are subject to strict network validation:
- **Private IP Blocking**: Blocks requests targeting `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254` (cloud metadata endpoints), and localhost aliases.
- **Same-Origin Redirects**: Redirects are only followed if the target shares the same protocol, host, and port as the initial validated URL.
- **Domain Allowlist / Blocklist**: Configurable in `settings.json` under `networkPolicy`.
