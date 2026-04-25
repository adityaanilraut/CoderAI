import type {AgentInfo, ToolCategory, ToolRisk, ReasoningEffort} from "../protocol.js";

export type TimelineItem =
  | {kind: "user"; id: string; text: string}
  | {kind: "assistant"; id: string; content: string; streaming: boolean; reasoning: string}
  | {
      kind: "tool";
      id: string;
      name: string;
      category: ToolCategory;
      args: Record<string, unknown>;
      risk: ToolRisk;
      ok: boolean | null;
      preview: string | null;
      error: string | null;
      fullAvailable: boolean;
    }
  | {kind: "diff"; id: string; path: string; diff: string}
  | {
      kind: "error";
      id: string;
      category: "provider" | "tool" | "internal" | "protocol";
      message: string;
      hint?: string;
      details?: string;
    }
  | {kind: "toast"; id: string; level: "info" | "warning" | "success"; message: string}
  | {
      kind: "approval";
      id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: ToolRisk;
      decided: "pending" | "approved" | "denied";
    };

export interface SessionState {
  connected: boolean;
  thinking: boolean;
  streaming: boolean;
  model: string;
  provider: string;
  cwd: string;
  version: string;
  autoApprove: boolean;
  reasoning: ReasoningEffort;
  /** Show reasoning, expanded tool cards, and per-state toasts. */
  verbose: boolean;
  ctxUsed: number;
  ctxLimit: number;
  costUsd: number;
  budgetUsd: number;
  agents: Record<string, AgentInfo>;
  /**
   * Wall-clock ms (Date.now()) at which an agent most recently flipped to
   * a terminal status (done/error/cancelled). Used by the AgentTree panel
   * to fade finished children out after a grace window. Root agents are
   * exempt from grace-based culling — see AgentTree.
   */
  agentsFinishedAt: Record<string, number>;
}
