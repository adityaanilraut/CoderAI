# CoderAI Tool Reference

This document provides a comprehensive catalog of all built-in tools available to the CoderAI model and agents.

---

## Tool Catalog Overview

| Category | Tools | Description |
|---|---|---|
| **File Operations** | `read`, `write`, `edit`, `str_replace_editor` | Scoped file reading, writing, and atomic snippet editing |
| **Search & Discovery** | `glob`, `grep` | Fast workspace file discovery via bundled ripgrep with spill handling |
| **Terminal & Processes** | `bash`, `job_list`, `job_output`, `job_kill`, `pwsh` | Shell execution, background job management, cross-platform PowerShell |
| **PTY Terminals** | `terminal_open`, `terminal_send`, `terminal_read`, `terminal_signal`, `terminal_close`, `terminal_list` | Interactive persistent terminal sessions |
| **Agent Orchestration** | `subagent`, `subagent_fork`, `send_message`, `interrupt_agent`, `list_agents` | Continuable background subagents and one-shot delegation |
| **Swarm & Teams** | `spawn_teammate`, `team_task_create`, `team_task_update`, `team_task_get`, `team_task_list`, `wait_agent` | Multi-agent collaboration with shared task board |
| **Language Server** | `lsp` | LSP code navigation (definitions, references, symbols) |
| **Planning & Goals** | `goal`, `todo_write`, `UpdatePlan`, `exit_plan_mode` | Session goals, plan mode checklists, and structured task management |
| **Workflows & Automation** | `workflow`, `ralph`, `code_mode`, `schedule_create`, `schedule_list`, `schedule_delete` | Scriptable pipelines, verification loop, in-process Python, and timers |
| **Web & Media** | `WebSearch`, `WebFetch`, `UnderstandImage` | Web search, URL fetching with SSRF protection, image understanding |
| **User Interaction** | `AskUserQuestion` | Interactive user questionnaires and decision modals |
| **Skills** | `skill` | Load discovered workspace skills (`SKILL.md`) |

## Tool Presets

The `--preset` CLI option, `toolsPreset` setting, and `CODERAI_TOOLS_PRESET` environment variable accept these canonical values:
- `full`: expose every built-in tool
- `core`: expose `bash`, `str_replace_editor`, `edit`, `read`, `write`, `glob`, and `grep`
- `shell_edit`: expose only `bash` and `str_replace_editor`

---

## 1. File Operations

### `read`
Reads file contents within line bounds and returns an anchored `snippet_id`.
- **Parameters**:
  - `file_path` (string, required): Relative or absolute path to the file.
  - `offset` (integer, optional): Starting line number (1-indexed). Default `1`.
  - `limit` (integer, optional): Maximum number of lines to return. Default `2000`.
- **Returns**: Anchored snippet identifier, line count, and snippet content.

### `edit`
Applies atomic string replacement strictly targeted to a verified `snippet_id`.
- **Parameters**:
  - `snippet_id` (string, required): The snippet identifier obtained from a previous `read`.
  - `old_str` (string, required): The exact text block to replace.
  - `new_str` (string, required): The replacement text block.
  - `replace_all` (boolean, optional): Set `true` if replacing multiple instances. Default `false`.
- **Guards**: Verifies file version against the snippet hash; aborts if the file was modified externally.

### `write`
Creates a new file or atomically overwrites an existing file.
- **Parameters**:
  - `file_path` (string, required): Target file path.
  - `content` (string, required): Content to write.

### `str_replace_editor`
Universal editor interface supporting `view`, `create`, `str_replace`, `insert`, and `undo_edit`.

---

## 2. Search & Discovery

### `glob`
Finds files matching wildcard glob patterns.
- **Parameters**:
  - `pattern` (string, required): Glob pattern (e.g. `**/*.py`, `src/**/*.ts`).
  - `path` (string, optional): Search root directory.
  - `limit` (integer, optional): Maximum results (default 100). Oversized results spill to disk.

### `grep`
Searches file contents using bundled high-performance ripgrep with Python regex fallback.
- **Parameters**:
  - `pattern` (string, required): Search regex or literal string.
  - `path` (string, optional): Target directory or file.
  - `case_sensitive` (boolean, optional): Case sensitivity. Default `true`.
  - `max_results` (integer, optional): Maximum match count (default 250).

---

## 3. Terminal & Process Management

### `bash`
Executes bash/sh commands with timeouts, process isolation, and background job support.
- **Parameters**:
  - `command` (string, required): Shell command.
  - `run_in_background` (boolean, optional): When `true`, spawns a background job and returns a `job_id`.
  - `timeout_s` (float, optional): Command timeout in seconds.

### `job_list` / `job_output` / `job_kill`
- `job_list`: Lists all running and completed background jobs.
- `job_output`: Reads streaming output from a background job with pagination offsets.
- `job_kill`: Sends termination signals to a running job and its child process tree.

### `pwsh`
Executes cross-platform PowerShell commands (Windows `powershell.exe` or Linux/macOS `pwsh`).

---

## 4. PTY Terminal Subsystem

- **`terminal_open`**: Spawns an interactive pseudoterminal session.
- **`terminal_send`**: Sends input or commands to a persistent terminal.
- **`terminal_read`**: Reads available stdout/stderr from the terminal buffer.
- **`terminal_signal`**: Sends a signal to a persistent terminal process.
- **`terminal_close`**: Closes a terminal session and frees resources.
- **`terminal_list`**: Lists all active terminal sessions.

---

## 5. Multi-Agent Orchestration

### `subagent`
Spawns a continuable background subagent with an isolated scratch workspace and dedicated turn loop.
- **Parameters**:
  - `description` (string, required): Objective of the subagent.
  - `mode` (string, optional): `read_only` or `general`.
  - `prompt` (string, optional): Initial instruction.

### `send_message` / `interrupt_agent` / `list_agents`
- `send_message`: Sends follow-up instructions to an active subagent.
- `interrupt_agent`: Gracefully interrupts a running subagent.
- `list_agents`: Lists active and completed subagents with token metrics.

### `spawn_teammate` & Team Task Board
- `spawn_teammate`: Spawns a specialized teammate agent for collaborative problem solving.
- `team_task_create` / `team_task_update` / `team_task_get` / `team_task_list`: Shared task board for multi-agent coordination.
- `wait_agent`: Waits for a teammate agent to finish its assigned task.

---

## 6. Language Server Protocol (`lsp`)

Inspects symbols and definitions via LSP language servers.
- **Actions**: `definition`, `references`, `document_symbols`, `workspace_symbols`.

---

## 7. Planning & Goals

### `goal`
Manages high-level session goals and deliverables.
- **Actions**: `list`, `add`, `update`, `start`, `done`, `cancel`.

### `todo_write`
Updates structured todo items and task breakdowns.

### `exit_plan_mode`
Exits Plan Mode when exploration and user approval are complete, enabling execution without modifying tool schemas.

---

## 8. Workflows & Automation

### `workflow`
Executes multi-phase asynchronous Python workflow scripts with logging and parallel execution.

### `ralph`
Automated verification harness running test-driven feedback loops to confirm task completion.

### `code_mode`
Executes in-process Python code for fast data processing, calculations, and transformation tasks.

### `schedule_create` / `schedule_list` / `schedule_delete`
Schedules one-shot timers or recurring cron notifications.

---

## 9. Web & Network

### `WebSearch`
Performs web searches across pluggable search providers (DuckDuckGo, Google, Bing).

### `WebFetch`
Fetches a webpage and sanitizes HTML into readable Markdown with SSRF validation, private IP blocking, and same-origin redirect policies.

### `UnderstandImage`
Inspects local PNG, JPEG, GIF, and WebP images.
