# CoderAI CLI & Command Reference

This reference covers the command-line flags and interactive slash commands available in CoderAI.

---

## Command-Line Arguments

```text
usage: coderai [-h] [--version] [-p PROMPT] [-e [PROMPT]] [--agent AGENT]
               [--agent-file AGENT_FILE] [--model MODEL] [--preset PRESET]
               [--yolo] [--afk] [--plan] [--resume [SESSION_ID]]
               [--fork [SESSION_ID]] [--list-sessions] [--setup]
```

### Options & Flags

| Flag | Description |
|---|---|
| `-p, --prompt <text>`, `-c, --command <text>` | Submit a prompt on launch (a bare positional prompt works too). |
| `-e, -x, --exec [prompt]` | Run one prompt non-interactively (requires `--prompt`/`-p` or a positional prompt). |
| `--agent <name>` | Launch with a specific agent spec or discovered role (`architect`, `tdd-guide`, etc.). |
| `--agent-file <path>` | Path to a custom YAML or Markdown agent spec file. |
| `-m, --model <model>` | Override the default model (e.g. `gpt-4o`, `claude-3-7-sonnet`). |
| `--preset <preset>` | Tool preset: `full`, `core`, or `shell_edit`. `--permission` and `--tools-preset` are aliases of this flag. Sandbox presets (`read-only`, `workspace-write`, `danger-full-access`) are separate and set with `/permission`. |
| `--yolo, --yes, -y, --auto-approve` | Run in YOLO mode (auto-approve all tool actions). |
| `--afk` | Run in AFK mode (auto-pilot with background notification alerts). |
| `--plan` | Launch directly into Plan Mode. |
| `-r, -s, --resume, --session [id]` | Resume a session by ID, prefix, or checkpoint (no ID shows the session picker). |
| `-f, --fork [id]` | Fork a session at its current state (defaults to the most recent). |
| `-l, --last` | Resume the most recent session for the current project directory. |
| `-C, --continue` | Continue the previous session for the working directory. |
| `--list-sessions` | Display existing local sessions. |
| `-w, --work-dir <path>` | Working directory for the agent (default: current directory). |
| `--add-dir <path>` | Add an additional directory to the workspace scope (repeatable). |
| `--skills-dir <path>` | Custom skills directories (repeatable, overrides default discovery). |
| `--print` | Run non-interactively (implies AFK auto-approval for this invocation). |
| `-q, --quiet` | Alias for `--print` with minimal output (final message only). |
| `--final-message-only` | Only print the final assistant message (requires `--print`). |
| `--thinking` / `--no-thinking` | Enable/disable thinking mode for this invocation. |
| `--reasoning-effort <level>` | Reasoning effort: `high`, `medium`, `low`, `off`, `xhigh`, `max`. |
| `--trust-project` | Allow project .env, hooks, and MCP server commands (default prompt in TTY). |
| `--no-trust-project` | Force untrusted mode for this project. |
| `--setup` | Launch interactive LLM provider and API key setup wizard. |
| `--provider <name>` | Provider to configure in setup (`openai`, `deepseek`, `gemini`, `anthropic`, `openrouter`, `ollama`). |
| `--key <key>` | API key to save for the specified provider. |
| `--base-url <url>` | Base URL endpoint for local or custom providers. |
| `--setup-model <model>` | Default model to configure in setup. |
| `--test` | Test connection and authentication with the active or specified provider. |
| `--status` | Display provider credentials and configuration status table. |
| `--project` | Save configuration to the project workspace instead of user global. |
| `--global` | Save configuration to user global settings (`~/.coderai`). |
| `--max-steps-per-turn <n>` | Maximum number of steps in one turn. |
| `--max-retries-per-step <n>` | Maximum number of retries in one step. |
| `--max-ralph-iterations <n>` | Extra iterations after the first turn in Ralph mode (`-1` for unlimited). |
| `--max-subagent-depth <n>` | Maximum sub-agent nesting depth (default 3). |
| `--subagent-timeout <secs>` | Sub-agent timeout in seconds (default 90). |
| `--max-continuable-agents <n>` | Maximum live continuable sub-agents per session (default 50). |
| `--max-running-jobs <n>` | Maximum concurrent running background jobs per session (default 50). |
| `--mcp-config <json>` | MCP config JSON string to load (repeatable; highest precedence). |
| `--mcp-config-file <path>` | MCP config file to load (repeatable). |
| `--config <json>` | Config JSON string to load (overrides files). |
| `--config-file <path>` | Config file to load instead of `~/.coderai/settings`. |
| `--input-format <fmt>` | Input format with `--print` reading stdin: `text`, `stream-json`. |
| `--output-format <fmt>` | Output format with `--print`: `text`, `stream-json`, `json`. |
| `--wire` | Serve the wire protocol (JSON-RPC) on stdio for IDE/headless clients. |
| `--debug` | Log debug information. |
| `-v, --verbose` | Print debug information. |

The `--subagent-*` worker flags (`--subagent-worker`, `--subagent-payload`,
`--subagent-depth`, `--subagent-parent-id`, `--subagent-runner`,
`--subagent-type`, `--subagent-desc`, `--subagent-allowed-tools`) are internal
plumbing for spawned subagent processes, not user-facing options.

---

## Interactive Slash Commands

Inside the interactive REPL shell, the following commands are available:

| Slash Command | Description |
|---|---|
| `/help [cmd]` | Display help and usage examples for all slash commands. |
| `/agents` | List the live subagent tree for the current session. |
| `/agents roles` | Display all bundled and discovered agent roles (`.coderai/agents/*.md`). |
| `/agents tree` | Same live tree as `/agents`. |
| `/agents report <id>` | View the report from a live subagent. |
| `/agents send <id> <message>` | Queue a follow-up message for a live subagent. |
| `/teams` | Inspect active multi-agent team teammates, status, and task board. |
| `/plan` | Enter or toggle Plan Mode (read-only architectural planning). |
| `/yolo` | Toggle YOLO mode on/off. |
| `/afk` | Toggle AFK auto-pilot mode on/off. |
| `/model [name]` | View or switch active LLM model. |
| `/undo` | Roll back the last turn's file modifications. |
| `/diff` | View uncommitted changes in the repository. |
| `/review [base] [--all]` | Review uncommitted changes (or changes since `merge-base(base, HEAD)`): Jev System-One skips low-risk files, the active model reviews the rest, and Jev drops speculative comments. Works without `TYPESAFE_API_KEY` by reviewing every non-doc file. |
| `/compact` | Trigger manual session history compaction. |
| `/skills` | List discovered workspace skills and cheat sheets. |
| `/skill <name>` | Load a skill into this session. |
| `/clear` | Clear the terminal display. |
| `/exit` | Exit the interactive session. |
| `/new` | Start a fresh session. |
| `/sessions` | Browse, resume, delete, or fork sessions. |
| `/delete <id>` | Delete a saved session. |
| `/rename [title]` | View or set the session title. |
| `/fork [id]` | Fork the current or specified session. |
| `/history` | Show the session timeline. |
| `/reset` | Clear conversation context and reset session state. |
| `/context` | Inspect live context window utilization. |
| `/debug` | Show context debug info (msgs/tokens/checkpoints). |
| `/tokens` | Show token usage breakdown. |
| `/usage` | Show API usage / quota. |
| `/effort [level]` | Select reasoning effort. |
| `/thinking` | Toggle reasoning trace display. |
| `/config` | Show resolved configuration. |
| `/setup` | Run interactive setup wizard for providers, keys, and models. |
| `/login` / `/logout` | Log in / configure, or log out from, the API platform. |
| `/permission [preset]` | Show or set the permission preset. |
| `/add-dir <path>` | Add directory to workspace. |
| `/editor` | Compose a prompt in external `$EDITOR`. |
| `/paste` | Enter multiline paste mode. |
| `/import <file>` | Import context from a file. |
| `/export` | Export session history to Markdown or JSON. |
| `/jobs` | Inspect and manage background jobs. |
| `/task` | Open interactive background-task browser. |
| `/schedule` | Manage reminders and background timers. |
| `/goal` | List or update session goals. |
| `/hooks` | Show configured hooks. |
| `/mcp` | Inspect MCP servers, tools, prompts, and resources. |
| `/continue` | Continue agent execution. |
| `/btw` | Ask a lightweight side question without mutating the main conversation history. |
| `/image` | Attach an image for analysis. |
| `/doctor` | Run system and connectivity diagnostics. |
| `/theme` | Switch theme dark/light. |
| `/agent [name]` | View or switch active agent role. |
| `/changelog` | Show recent changelog. |
| `/version` | Show CLI version. |
| `/upgrade` | Check for and install CoderAI updates. |
| `/feedback <text>` | Submit feedback. |
| `/reload` | Reload configuration without exiting. |
| `/init [focus]` | Queue a turn that writes or updates AGENTS.md from the project. |

---

## Subcommands

Subcommands are dispatched before the interactive parser, so they work with no
configuration and never enter the REPL:

| Subcommand | Description |
|---|---|
| `coderai acp` | Run the Agent Control Protocol (ACP) server on stdio. |
| `coderai info [--json]` | Print resolved environment, config, and session info. |
| `coderai export [id] [-o path] [--yes]` | Export a session to Markdown, JSON, or a zip archive. |
| `coderai mcp <add\|remove\|list\|login\|auth>` | Manage stdio/HTTP MCP servers and their credentials. |
| `coderai plugin <list\|install\|remove\|status>` | Manage plugins. |
| `coderai login` / `coderai logout` | Configure or clear provider credentials (OAuth or API key). |

The same entry points are reachable via the module form when running from a
source checkout, e.g. `python -m coderai.cli acp`.

### ACP server

`coderai acp` speaks JSON-RPC 2.0 over newline-delimited JSON on stdin/stdout,
so it can be launched directly by an ACP-capable editor or agent client. The
`initialize` response advertises session `resume`, `fork`, and `list`
capabilities plus image and embedded-context prompt support.

