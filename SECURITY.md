# Security Policy

CoderAI is an AI-powered pair programming agent designed for the terminal. It reads and writes files, executes shell commands, performs web searches, and interacts with Model Context Protocol (MCP) servers on your local machine.

This document outlines the CoderAI security model, side-effect permission scopes, sandbox boundaries, and vulnerability reporting procedures.

---

## Reporting a Vulnerability

If you discover a security vulnerability in CoderAI, please report it **privately**:

- Open a private security advisory via GitHub under the repository's **Security** tab, or
- Contact the maintainers directly at **adityaanilraut@gmail.com**.

Please include:

1. Description of the issue and affected version(s).
2. Step-by-step reproduction steps or proof-of-concept.
3. Impact assessment.

We strive to acknowledge reports promptly and coordinate a fix prior to public disclosure.

---

## Threat Model & Core Principles

The primary threat model for an autonomous coding agent is **unintended, unauthorized, or untrusted execution of privileged local actions**.

CoderAI implements the following core security principles:

1. **Explicit Permission Scopes**: Every tool action declares its side-effect scopes prior to execution.
2. **Fail-Closed Default**: Actions that cannot be safely evaluated or that violate explicit policy are denied.
3. **Snippet-Scoped File Operations**: File modifications are bound to verified snippet identifiers, preventing blind hallucinations and race conditions.
4. **Isolated Plan Mode Boundary**: Planning phases strictly enforce read-only execution unless explicitly authorized by the user.
5. **OS Sandboxing**: Shell commands are executed inside native OS sandboxes (`sandbox-exec` on macOS, `bwrap` on Linux) under non-privileged presets.
6. **PreToolUse Hooks**: Optional validation scripts run prior to tool execution with fail-closed denial.
7. **Git Checkpointing & Instant Undo**: File modifications are checkpointed per turn, allowing immediate rollback of inadvertent changes.
8. **Subprocess Isolation & Timeout Bounds**: Shell executions enforce strict timeouts, signal traps, and process tree termination to prevent runaway processes.
9. **Network & SSRF Guard**: Outbound web requests enforce private IP filters, loopback blocking, and same-origin redirect verification.

---

## Security Controls

### 1. Granular Side-Effect Permissions (`coderai/core/permissions.py`)

CoderAI classifies all operations into granular side-effect scopes:

| Scope            | Description                                                    | Risk Level |
| ---------------- | -------------------------------------------------------------- | ---------- |
| `read-in-cwd`    | Reading files within the workspace root                        | Low        |
| `read-out-cwd`   | Reading files outside the workspace root                       | Medium     |
| `write-in-cwd`   | Creating or modifying files within the workspace root          | Medium     |
| `write-out-cwd`  | Creating or modifying files outside the workspace root         | High       |
| `delete-in-cwd`  | Deleting files within the workspace root                       | High       |
| `delete-out-cwd` | Deleting files outside the workspace root                      | Critical   |
| `query-git-log`  | Inspecting git status, diffs, or logs                          | Low        |
| `mutate-git-log` | Altering git history (commit, branch, reset, checkout, rebase) | High       |
| `network`        | Outbound HTTP requests and web searches                        | Medium     |
| `mcp`            | Calling tools exposed by external MCP servers                  | Medium     |

#### Permission Presets

CoderAI supports 3 standard presets for fast and consistent security configuration:

- **`read-only`**: Blocks all mutations and confines execution to read operations with an active OS sandbox.
- **`workspace-write`**: Grants write access only within the workspace root with an active OS sandbox.
- **`danger-full-access`**: Full access with interactive user confirmation for sensitive operations.

#### Custom Permission Policies

Permissions can also be customized in `~/.coderai/settings.json` (user-level) or `.coderai/settings.json` (project-level):

```json
{
  "permissions": {
    "preset": "workspace-write",
    "allow": ["read-in-cwd", "query-git-log"],
    "ask": [
      "write-in-cwd",
      "write-out-cwd",
      "delete-in-cwd",
      "mutate-git-log",
      "network",
      "mcp"
    ],
    "deny": ["delete-out-cwd"]
  }
}
```

- **`allow`**: Automatically approve tool execution matching these scopes.
- **`ask`**: Display a rich confirmation card detailing command, arguments, and affected paths before running.
- **`deny`**: Block execution unconditionally with a security denial message.
- **CLI Override**: Passing `--yes` or `-y` runs in auto-approval mode for trusted, automated workflows.

---

### 2. OS-Level Sandboxing (`coderai/core/sandbox.py`)

For `read-only` and `workspace-write` presets, shell commands are wrapped in native OS sandboxes:

- **macOS Seatbelt**: Uses `sandbox-exec` with a compiled profile allowing read operations and restricting file writes strictly to the workspace root and temporary directories.
- **Linux Bubblewrap (`bwrap`)**: Isolates the process in private mount namespaces with a read-only root and writeable workspace binds.
- **Fallback**: If OS sandbox tools are not available on the host platform, commands fall back to interactive user confirmation (`ask`).

---

### 3. PreToolUse Hooks (`coderai/core/hooks.py`)

Organizations can define custom PreToolUse hook scripts in `.coderai/hooks.json` to enforce compliance and security checks:

```json
{
  "preToolUse": [
    {
      "command": "python scripts/security_check.py --tool {tool_name}",
      "failAction": "deny"
    }
  ]
}
```

---

### 4. Snippet-Scoped Editing & Version Guarding (`coderai/core/state.py`, `coderai/core/tools/edit.py`)

To prevent hallucinated edits or accidental full-file corruption:

- An edit must supply a valid `snippet_id` received from a preceding `read` call.
- The `StateManager` verifies that the target file has not been externally modified since the snippet was captured.
- Target strings are matched deterministically with whitespace tolerance and multi-occurrence guards (`replace_all` requirement).
- If a file version mismatch is detected, the edit is aborted and the agent is instructed to re-read the active file window.

---

### 5. Plan Mode Capability Fence (`coderai/core/prompt.py`, `coderai/core/permissions.py`)

When Plan Mode is enabled (`/plan` in REPL or `--plan` on CLI):

- Mutating scopes (`write-in-cwd`, `write-out-cwd`, `delete-in-cwd`, `delete-out-cwd`, `mutate-git-log`) are forced into `ask` mode regardless of `allow` settings.
- The agent prompt is restricted to read-only exploration, architectural analysis, and task breakdown updates (`UpdatePlan`).
- Changes cannot be applied until the user reviews and confirms the proposed strategy.

---

### 6. Git-Backed Checkpointing & Recovery (`coderai/core/common/file_history.py`)

Every tool-driven filesystem modification is tracked in a local Git-backed history store (`.git_history`):

- **Automatic Checkpointing**: State snapshots are recorded before and after each user turn.
- **Unified Diff Inspection**: `/diff` displays exact line changes made during the active session.
- **Deterministic Undo**: `/undo` rolls back modified files to the exact state of the previous turn.

---

### 7. Process Lifecycle & Shell Hygiene (`coderai/core/tools/bash.py`, `coderai/core/common/`)

Shell execution is guarded by multiple defense layers:

- **Command Inspection**: `shell_utils.py` parses commands to detect side effects (filesystem writes, git mutations, background spawning).
- **Timeouts**: `bash_timeout.py` enforces maximum runtimes per command to prevent deadlocks.
- **Process Tree Cleanup**: `process_tree.py` traverses and terminates entire child process groups upon cancellation or timeout.
- **Background Job Tracking**: Background tasks (`run_in_background`) are given unique process IDs, output logs, and non-blocking status tracking.

---

### 8. Network Security & SSRF Protection (`coderai/core/network/`)

- **SSRF Validation**: Blocks requests targeting `127.0.0.1`, private RFC 1918 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local metadata addresses (`169.254.169.254`), and loopback addresses.
- **Same-Origin Redirects**: Redirects are only followed if the destination URL shares the identical protocol, host, and port of the initial validated URL.
- **HTML Sanitization**: Responses are stripped of script tags, tracking pixels, and malicious payloads before markdown conversion.

---

### 9. Model Context Protocol (MCP) Security (`coderai/core/mcp/`)

- MCP servers communicate over `stdio`, `sse`, or `streamable-http`.
- Server executables and argument lists are explicitly specified in configuration.
- Tool names are strictly namespaced (`mcp__<server>__<tool>`) to prevent collisions with native tools.

---

### 10. Storage and Credential Protection

- **Local Session Storage**: Session transcripts, indexes, and checkpoints are stored in user-owned directories (`~/.coderai/projects/<project-hash>/`).
- **Environment Isolation**: API keys configured via environment variables (`OPENAI_API_KEY`, `CODERAI_API_KEY`) are read strictly within client initialization and never logged to disk or serialized in session histories.

---

## Supported Environments

| Platform                          | Support Tier                        |
| --------------------------------- | ----------------------------------- |
| **macOS (Apple Silicon & Intel)** | Tier 1 (Fully Supported & Verified) |
| **Linux (x86_64, aarch64)**       | Tier 1 (Fully Supported & Verified) |
| **Windows (WSL2 / PowerShell)**   | Tier 2 (Supported)                  |

---

## Maintenance and Updates

CoderAI receives regular security updates on the `main` branch. Users are encouraged to stay up to date:

```bash
pip install -U coderai-agent
```
