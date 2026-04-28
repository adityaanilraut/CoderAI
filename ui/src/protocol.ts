/**
 * Shared IPC protocol types. Keep this in sync with `ui/PROTOCOL.md`
 * and the Python emitter in `coderAI/ipc/jsonrpc_server.py`.
 */

export type ToolCategory =
  | "fs"
  | "git"
  | "shell"
  | "web"
  | "search"
  | "agent"
  | "mcp"
  | "other";

export type ToolRisk = "low" | "medium" | "high";

export type ReasoningEffort = "high" | "medium" | "low" | "none";

/** Aligned with `AgentStatus` in `coderAI/agent_tracker.py` (emitted in `agent` / `agent_lifecycle` payloads). */
export type AgentStatus =
  | "idle"
  | "thinking"
  | "tool_call"
  | "waiting_for_user"
  | "done"
  | "error"
  | "cancelled";

export interface AgentInfo {
  id: string;
  name: string;
  role: string | null;
  parentId: string | null;
  status: AgentStatus;
  task: string;
  tool: string | null;
  model: string;
  tokens: number;
  costUsd: number;
  ctxUsed: number;
  ctxLimit: number;
  elapsedMs: number;
}

export type AgentEvent =
  | {
      event: "hello";
      model: string;
      provider: string;
      cwd: string;
      version: string;
      contextLimit: number;
      budgetLimit: number;
      autoApprove: boolean;
    }
  | { event: "ready" }
  | { event: "turn"; phase: "start" | "reasoning" | "text" | "end"; delta?: string; elapsedMs?: number }
  | { event: "tool"; id: string; phase: "queued" | "awaiting_approval" | "running" | "ok" | "err" | "cancelled"; payload: Record<string, any> }
  | { event: "file_diff"; path: string; diff: string }
  | {
      event: "status";
      ctxUsed: number;
      ctxLimit: number;
      costUsd: number;
      budgetUsd: number;
      promptTokens: number;
      completionTokens: number;
      totalTokens: number;
    }
  | { event: "agent"; phase: "update"; info: AgentInfo; parentId: string | null }
  | {
      event: "error";
      category: "provider" | "tool" | "internal" | "protocol";
      message: string;
      hint?: string;
      details?: string;
    }
  | { event: "session_patch"; model?: string; provider?: string; autoApprove?: boolean; reasoning?: ReasoningEffort }
  | { event: "available_models"; current: string; models: Record<string, string[]> }
  | { event: "progress"; label: string; current?: number; total?: number; progressKind: "tokens" | "files" | "steps" }
  | { event: "info"; message: string }
  | { event: "warning"; message: string }
  | { event: "success"; message: string }
  | { event: "goodbye"; reason?: string };

/** Every `event` name the agent may send; used for dev checks in `agentClient`. Keep in sync with this union. */
export const AGENT_EVENT_NAMES: readonly string[] = [
  "hello",
  "ready",
  "turn",
  "tool",
  "file_diff",
  "status",
  "agent",
  "error",
  "session_patch",
  "available_models",
  "progress",
  "info",
  "warning",
  "success",
  "goodbye",
];

export type UIEnvelope = { v: 1; kind: "event" } & AgentEvent;

export type VerbosityLevel = "quiet" | "normal" | "verbose";

// AgentCommand lists every command the UI may send over the NDJSON bridge;
// see `AgentClient.send()` for the runtime envelope shape.
export type AgentCommand =
  | { cmd: "send_message"; text: string }
  | { cmd: "cancel"; agentId?: string }
  | { cmd: "tool_approval_resp"; toolId: string; approve: boolean }
  | { cmd: "set_model"; model: string }
  | { cmd: "set_reasoning"; effort: ReasoningEffort }
  | { cmd: "set_default_model"; model: string }
  | { cmd: "set_verbosity"; level: VerbosityLevel }
  | { cmd: "toggle_auto_approve" }
  | { cmd: "compact_context" }
  | { cmd: "clear_context" }
  | { cmd: "get_state" }
  | { cmd: "get_plan" }
  | { cmd: "list_models" }
  | { cmd: "reference"; topic: string }
  | { cmd: "exit" };
