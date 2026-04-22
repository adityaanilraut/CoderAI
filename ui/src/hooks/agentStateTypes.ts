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
      category: "provider" | "tool" | "internal";
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
    }
  | {kind: "agent"; id: string; agent: AgentInfo};

export interface SessionState {
  connected: boolean;
  thinking: boolean;
  streaming: boolean;
  model: string;
  provider: string;
  cwd: string;
  version: string;
  projectSummary?: string;
  autoApprove: boolean;
  reasoning: ReasoningEffort;
  ctxUsed: number;
  ctxLimit: number;
  costUsd: number;
  budgetUsd: number;
  agents: Record<string, AgentInfo>;
}
