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
      projectSummary?: string;
      contextLimit: number;
      budgetLimit: number;
      autoApprove: boolean;
    }
  | { event: "ready" }
  | { event: "assistant_start" }
  | { event: "stream_delta"; content: string; reasoning?: boolean }
  | { event: "assistant_end"; content: string }
  | { event: "thinking_start" }
  | { event: "thinking_end"; elapsedMs: number }
  | {
      event: "tool_call";
      id: string;
      name: string;
      category: ToolCategory;
      args: Record<string, unknown>;
      risk: ToolRisk;
    }
  | {
      event: "tool_result";
      id: string;
      ok: boolean;
      preview: string;
      fullAvailable: boolean;
      error?: string;
    }
  | {
      event: "tool_approval_req";
      id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: ToolRisk;
    }
  | {
      event: "tool_approval_timeout";
      id: string;
      tool: string;
      timeoutSeconds: number;
    }
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
  | { event: "agent_update"; agent: AgentInfo }
  | {
      event: "agent_lifecycle";
      action: "started" | "finished";
      agent: AgentInfo;
    }
  | {
      event: "error";
      category: "provider" | "tool" | "internal" | "protocol";
      message: string;
      hint?: string;
      details?: string;
    }
  | { event: "info"; message: string }
  | { event: "warning"; message: string }
  | { event: "success"; message: string }
  | { event: "model_changed"; model: string; provider: string }
  | { event: "auto_approve_changed"; autoApprove: boolean }
  | { event: "reasoning_changed"; effort: ReasoningEffort }
  | { event: "goodbye"; reason?: string };

/** Every `event` name the agent may send; used for dev checks in `agentClient`. Keep in sync with this union. */
export const AGENT_EVENT_NAMES: readonly string[] = [
  "hello",
  "ready",
  "assistant_start",
  "stream_delta",
  "assistant_end",
  "thinking_start",
  "thinking_end",
  "tool_call",
  "tool_result",
  "tool_approval_req",
  "tool_approval_timeout",
  "file_diff",
  "status",
  "agent_update",
  "agent_lifecycle",
  "error",
  "info",
  "warning",
  "success",
  "model_changed",
  "auto_approve_changed",
  "reasoning_changed",
  "goodbye",
];

export type UIEnvelope = { v: 1; kind: "event" } & AgentEvent;

// AgentCommand lists every command the UI may send over the NDJSON bridge;
// see `AgentClient.send()` for the runtime envelope shape.
export type AgentCommand =
  | { cmd: "send_message"; text: string }
  | { cmd: "cancel"; agentId?: string }
  | { cmd: "tool_approval_resp"; toolId: string; approve: boolean }
  | { cmd: "set_model"; model: string }
  | { cmd: "set_reasoning"; effort: ReasoningEffort }
  | { cmd: "set_default_model"; model: string }
  | { cmd: "toggle_auto_approve" }
  | { cmd: "compact_context" }
  | { cmd: "clear_context" }
  | { cmd: "get_state" }
  | { cmd: "get_plan" }
  | { cmd: "reference"; topic: string }
  | { cmd: "exit" };
