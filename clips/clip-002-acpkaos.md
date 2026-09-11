---
Status: Implemented
Date: 2026-09-11
---

# CLIP-002: ACPKaos — Agent Control Protocol Server

Agent Control Protocol (ACP) specification for CoderAI: in-process JSON-RPC
server, pseudo-terminal (PTY) isolation, and extensible method dispatch.

## Summary

`coderai/acp/` implements an in-process JSON-RPC ACP server. Editor
extensions and headless daemons create sessions, stream prompts, cancel
turns, fork branches, and call extension methods — all without the terminal
REPL. Tool execution that touches the outside world (file reads/writes,
terminal commands) is redirected to ACP client-advertised capabilities when
present, and falls back to local execution otherwise.

## Motivation

- Editors (e.g. Zed-class ACP clients) want to *observe* file edits and
  command execution natively instead of parsing terminal output.
- The KAOS abstraction already centralizes OS operations; ACP fits as a
  backend behind it rather than as duplicated per-tool logic.
- One session engine must serve interactive and programmatic clients with
  identical semantics.

## Session lifecycle (`coderai/acp/server.py`)

| Method | Purpose |
|---|---|
| `initialize` | Capability handshake; client advertises `fs/*`, `terminal/*` |
| `authenticate` (`login`) | Token auth; `auth_required` error until complete |
| `new_session` | Create session (`_setup_session` wires engine + KAOS backend) |
| `load_session` / `resume_session` | Rehydrate persisted event stream |
| `fork_session` | Branch at current state; original untouched |
| `list_sessions` | Enumerate share-dir sessions |
| `set_session_mode` | Toggle yolo/afk/plan per session |
| `set_session_model` | Hot-swap model mid-session |
| `prompt` | Dispatch a turn; streams wire events back |
| `cancel` | Interrupt the running turn (→ `StepInterrupted`) |
| `ext_method` | Namespaced extension dispatch for client-specific calls |

Auth model: `_auth_methods` populated at `initialize`; `_check_auth()`
raises `RequestError.auth_required` with the method list until
`authenticate(login)` succeeds. Token usability is probed by
`_check_token_usable()` without triggering refresh.

## PTY isolation

Terminal tools execute inside a pseudo-terminal owned by the session so
ACP `terminal/*` clients can attach, observe, and (where advertised) feed
input. Local fallback uses the same PTY path — the only difference is who
holds the master fd. This keeps `Shell` behavior identical across REPL,
ACP, and `--wire` consumers.

## Method dispatch

`ext_method` routes `"<namespace>/<method>"` to registered handlers with
the session's approval context attached, so extensions inherit YOLO/AFK/
plan-mode gating for free. Unknown methods return `invalid_params`, never
a silent no-op.

## Non-goals

- No multi-tenant isolation beyond per-session KAOS scoping; the daemon
  trusts the local user.
- No wire auth on the event stream itself (see CLIP-001); auth terminates
  at session methods.
