# CoderAI Architectural Reference & Parity Matrix

CoderAI is engineered to deliver complete, enterprise-grade AI pair programming with frontier LLM support, rich terminal interactivity, and strict execution safety.

---

## Feature Comparison & Capabilities

| Capability | CoderAI | Conventional Agents |
|---|---|---|
| **Core Architecture** | UI-agnostic engine (`coderai.core`) + Rich TUI (`coderai.cli`) + JSON-RPC Server | Monolithic or Web-only |
| **Editing Mechanism** | Snippet-scoped anchored edits (`snippet_id`) with file version hashes | Full-file rewrites or unanchored fuzzy diffs |
| **Undo & Checkpointing** | Automatic Git-backed turn checkpoints with `/undo` and `/diff` | Destructive file modifications without rollback |
| **Search & Discovery** | Native `glob` & `grep` with bundled ripgrep & spill-to-disk locators | Ad-hoc shell `find` / `grep` commands |
| **Terminal Integration** | Background jobs (`job_*`), PTY terminals (`terminal_*`), PowerShell (`pwsh`) | Basic synchronous subshell only |
| **Agent Orchestration** | Continuable subagents, agent swarms (`spawn_teammate`), task boards, Ralph verification | Single-agent or brittle subagent forks |
| **Safety & Sandboxing** | 10 side-effect scopes, presets, OS sandbox (Seatbelt/bwrap), PreToolUse hooks | Unchecked command execution |
| **MCP Support** | Stdio, SSE, Streamable-HTTP with tools, prompts, and resources | Stdio only or missing prompts/resources |
| **Context Management** | Event-stream JSONL, token gauges, compaction with summary synthesis | Naive truncation or full context blowout |
| **Web Subsystem** | Pluggable search providers, SSRF guard, private IP filter, same-origin redirects | Unsanitized network calls |

---

## Subsystem Architecture Status

All core subsystems are fully implemented, verified, and backed by comprehensive automated test suites:

1. **Core Engine**: Append-only event stream, bounded execution loop, token compaction, system prompt section composition, multi-provider LLM routing.
2. **File & Search Tooling**: Snippet-anchored editing, full-text grep/glob with bundled binary support, spill file management.
3. **Safety & Permissions**: Granular scope enforcement, OS sandbox isolation, user approval modals, PreToolUse hooks.
4. **Agent Collaboration**: Background subagents, inter-agent messaging, agent team task boards, Ralph verification harness.
5. **Developer Workflows**: Language Server Protocol (LSP), PTY terminals, async workflow scripts, sandboxed Python code mode, session query indexer.
6. **IDE & Protocol Bridge**: Headless JSON-RPC 2.0 companion server mode for editor integrations.
