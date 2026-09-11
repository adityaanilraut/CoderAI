---
Status: Implemented
Date: 2026-09-11
---

# CLIP-001: Wire Protocol

Formal specification of CoderAI's asynchronous wire protocol: event types,
chunk routing, and the streaming lifecycle between the agent engine and its
consumers (terminal REPL, ACP clients, `--wire` stdio peers).

## Summary

The wire layer (`coderai/wire/`) is a one-way event stream: the engine emits
typed events, consumers render or forward them. There is no request/response
turn-taking on the wire itself — control flows back via narrow ingress
messages (`SteerInput`, `ApprovalResponse`, `QuestionResponse`).

## Motivation

- One engine must serve three consumers (Rich terminal, ACP JSON-RPC
  clients, headless `--wire` peers) without per-consumer branching.
- Streaming LLM output, tool execution, approvals, and interruptions must
  interleave deterministically so replays and tests observe total order.
- `tests_e2e/` drives the protocol with `FakeKimiSoul` — no live LLM needed.

## Event taxonomy (`coderai/wire/types.py`)

| Family | Events |
|---|---|
| Turn lifecycle | `TurnBegin`, `TurnEnd` |
| Step lifecycle | `StepBegin`, `StepInterrupted`, `StepRetry` |
| Content | `TextPart`, `ThinkPart`, `ToolCallPart`, `ToolResultPart` |
| Steering | `SteerInput` (ingress), `StatusUpdate`, `Notification` |
| Compaction | `CompactionBegin`, `CompactionEnd` |
| Hooks | `HookTriggered`, `HookResolved` |
| MCP | `MCPLoadingBegin`, `MCPLoadingEnd`, `MCPServerSnapshot`, `MCPStatusSnapshot` |
| Plan UI | `PlanDisplay`, `BtwBegin`, `BtwEnd` |
| Subagents | `SubagentEvent` |
| Approvals | `ApprovalRequest`, `ApprovalResponse` (ingress) |
| Questions | `QuestionItem`, `QuestionOption`, `QuestionResponse` (ingress) |

Serialization: `coderai/wire/serde.py`; transport framing over stdio/JSON-RPC:
`coderai/wire/jsonrpc.py`, `coderai/wire/server.py`; process-rooted fan-out:
`coderai/wire/root_hub.py`; file-backed replay: `coderai/wire/file.py`.

## Streaming lifecycle

```text
TurnBegin
  StepBegin
    ThinkPart* / TextPart*            # LLM chunks, in arrival order
    ToolCallPart                      # engine decides to act
    ApprovalRequest?                  # if policy requires confirmation
    ApprovalResponse (ingress)        # allow / deny
    ToolCallPart executes
    ToolResultPart                    # outcome re-enters context
    StepRetry* on transient failure
  StepInterrupted?                    # user steer / cancel lands here
  StepBegin ...                       # next step
TurnEnd
```

Rules:

1. Every `StepBegin` eventually resolves to `TurnEnd`, `StepRetry`, or
   `StepInterrupted` — no silent drops.
2. `ApprovalRequest` blocks its step until `ApprovalResponse`; YOLO/AFK
   modes synthesize the response engine-side (still emitted for observers).
3. `SteerInput` mid-step triggers `StepInterrupted` then re-plans; the
   interrupted step's partial chunks stay in history for replay fidelity.
4. `CompactionBegin/End` bracket history compression; downstream consumers
   must treat the summarized range as opaque.

## Non-goals

- No wire-level auth (handled above: ACP `authenticate`, share-dir identity).
- No backpressure protocol; the engine buffers and `root_hub` drops
  slowest-consumer overflow by design (terminal rendering is lossy, history
  is not — history comes from the session store, not the stream).
