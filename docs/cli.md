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
| `-p, --prompt <text>` | Run a single prompt non-interactively and exit. |
| `-e, --exec [prompt]` | Execute prompt non-interactively (alias for `-p`). |
| `--agent <name>` | Launch with a specific agent spec or discovered role (`architect`, `tdd-guide`, etc.). |
| `--agent-file <path>` | Path to a custom YAML or Markdown agent spec file. |
| `--model <model>` | Override the default model (e.g. `gpt-4o`, `claude-3-7-sonnet`). |
| `--preset <preset>` | Tool preset: `full`, `core`, or `shell_edit`. `--permission` and `--tools-preset` are aliases of this flag. Sandbox presets (`read-only`, `workspace-write`, `danger-full-access`) are separate and set with `/permission`. |
| `--yolo` | Run in YOLO mode (auto-approve all tool actions). |
| `--afk` | Run in AFK mode (auto-pilot with background notification alerts). |
| `--plan` | Launch directly into Plan Mode. |
| `--resume [id]` | Resume an existing session. |
| `--fork [id]` | Fork an existing session at its current state. |
| `--list-sessions` | Display existing local sessions. |
| `--setup` | Launch interactive LLM provider and API key setup wizard. |

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
| `/clear` | Clear the terminal display. |
| `/exit` | Exit the interactive session. |

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

