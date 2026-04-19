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
      category: "provider" | "tool" | "internal";
      message: string;
      hint?: string;
      details?: string;
    }
  | { event: "info"; message: string }
  | { event: "warning"; message: string }
  | { event: "success"; message: string }
  | { event: "model_changed"; model: string; provider: string }
  | { event: "auto_approve_changed"; autoApprove: boolean }
  | { event: "reasoning_changed"; effort: "high" | "medium" | "low" | "none" }
  | { event: "goodbye"; reason?: string };

export type UIEnvelope = { v: 1; kind: "event" } & AgentEvent;

export type AgentCommand =
  | { cmd: "send_message"; text: string }
  | { cmd: "cancel"; agentId?: string }
  | { cmd: "tool_approval_resp"; toolId: string; approve: boolean }
  | { cmd: "set_model"; model: string }
  | { cmd: "set_reasoning"; effort: "high" | "medium" | "low" | "none" }
  | { cmd: "toggle_auto_approve" }
  | { cmd: "compact_context" }
  | { cmd: "clear_context" }
  | { cmd: "get_state" }
  | { cmd: "exit" };

export type CmdEnvelope = { v: 1; kind: "cmd"; id: string } & AgentCommand;
