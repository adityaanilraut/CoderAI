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
      /**
       * Visual separator inserted by `appendCapped` when the timeline is
       * trimmed. Distinct from `toast` so it can render as a horizontal
       * rule instead of an inline notice — important context that older
       * history was dropped, not a transient message.
       */
      kind: "separator";
      id: string;
      message: string;
    }
  | {
      kind: "approval";
      id: string;
      tool: string;
      args: Record<string, unknown>;
      risk: ToolRisk;
      decided: "pending" | "approved" | "denied";
      /**
       * Unified-diff preview of the change about to be applied. Optional —
       * filesystem-mutating tools include it, terminal commands don't.
       */
      diff?: string;
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
  /** Per-turn token breakdown (set from status event). */
  promptTokens: number;
  /** Per-turn token breakdown (set from status event). */
  completionTokens: number;
  /** Model catalogue grouped by provider (populated by /model picker request). */
  availableModels: Record<string, string[]> | null;
  /** Available personas (populated by /persona command). */
  availablePersonas: string[] | null;
  /** Available skills (populated by /skills command). */
  availableSkills: { name: string; description: string }[] | null;
  /** Context files (populated by get_state). */
  contextFiles: { path: string; size: number }[] | null;
  agents: Record<string, AgentInfo>;
  /**
   * Wall-clock ms (Date.now()) at which an agent most recently flipped to
   * a terminal status (done/error/cancelled). Used by the AgentTree panel
   * to fade finished children out after a grace window. Root agents are
   * exempt from grace-based culling — see AgentTree.
   */
  agentsFinishedAt: Record<string, number>;
  /** Live progress bar state (null when nothing is in progress). */
  progress: {label: string; current?: number; total?: number; kind: "tokens" | "files" | "steps"} | null;
  /** Date.now() when the session started (set on hello). */
  sessionStartedAt: number | null;
}
