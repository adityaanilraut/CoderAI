/**
 * React hook that owns the AgentClient lifecycle and exposes:
 *   - session state (model, context %, cost, agents)
 *   - the message timeline (user inputs, assistant turns, tool cards, errors)
 *   - imperative helpers (send, cancel, setModel, ...)
 */

import {useEffect, useMemo, useRef, useState} from "react";
import {AgentClient} from "../rpc/agentClient.js";
import type {AgentInfo, ToolCategory, ToolRisk} from "../protocol.js";

export type ReasoningEffort = "high" | "medium" | "low" | "none";

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
    };

export interface SessionState {
  connected: boolean;
  ready: boolean;
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
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  agents: Record<string, AgentInfo>;
}

const INITIAL: SessionState = {
  connected: false,
  ready: false,
  thinking: false,
  streaming: false,
  model: "",
  provider: "",
  cwd: "",
  version: "",
  autoApprove: false,
  reasoning: "none",
  ctxUsed: 0,
  ctxLimit: 0,
  costUsd: 0,
  budgetUsd: 0,
  promptTokens: 0,
  completionTokens: 0,
  totalTokens: 0,
  agents: {},
};

/**
 * Maximum number of timeline items we keep in memory. Long sessions can
 * produce thousands of tool cards and Ink re-renders the whole tree on
 * every update, so we cap and prepend a "…older history truncated…" marker.
 */
const MAX_TIMELINE = 500;
const TRIM_TO = 400;

export interface UseAgentResult {
  session: SessionState;
  timeline: TimelineItem[];
  stderr: string;
  actions: {
    send: (text: string) => void;
    cancel: () => void;
    setModel: (m: string) => void;
    setReasoning: (effort: ReasoningEffort) => void;
    toggleAutoApprove: () => void;
    clearContext: () => void;
    compactContext: () => void;
    getState: () => void;
    approveTool: (toolId: string, approve: boolean) => void;
    exit: () => void;
    showHelp: () => void;
  };
}

export function useAgent(opts: {python?: string; cwd?: string} = {}): UseAgentResult {
  const clientRef = useRef<AgentClient | null>(null);
  const [session, setSession] = useState<SessionState>(INITIAL);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [stderr, setStderr] = useState<string>("");

  // Refs to avoid closure staleness inside event handlers.
  const readyRef = useRef(false);
  const goodbyeRef = useRef(false);
  const stderrRef = useRef("");
  // Tracks the index of the currently-streaming assistant item so
  // stream_delta doesn't have to scan the whole timeline.
  const currentAssistantIdx = useRef<number | null>(null);

  // Monotonic id generator shared across event handlers AND the synchronous
  // actions below. Kept on a ref so it survives re-renders.
  const uidRef = useRef(0);
  const nextId = () => `i_${++uidRef.current}_${Date.now().toString(36)}`;

  const push = (item: TimelineItem) =>
    setTimeline((prev) => appendCapped(prev, item));

  useEffect(() => {
    const client = new AgentClient({python: opts.python, cwd: opts.cwd});
    clientRef.current = client;

    client.on("hello", (m) => {
      setSession((s) => ({
        ...s,
        connected: true,
        model: m.model,
        provider: m.provider,
        cwd: m.cwd,
        version: m.version,
        projectSummary: m.projectSummary,
        ctxLimit: m.contextLimit,
        budgetUsd: m.budgetLimit,
        autoApprove: m.autoApprove,
      }));
    });

    client.on("ready", () => {
      readyRef.current = true;
      setSession((s) => ({...s, ready: true, thinking: false, streaming: false}));
    });

    client.on("thinking_start", () =>
      setSession((s) => ({...s, thinking: true})),
    );

    client.on("thinking_end", (m: {elapsedMs?: number}) => {
      setSession((s) => ({...s, thinking: false}));
      // Surface server-reported "thought for X s" for long pauses so the
      // record isn't lost when the Thinking spinner unmounts.
      if (typeof m.elapsedMs === "number" && m.elapsedMs >= 2000) {
        push({
          kind: "toast",
          id: nextId(),
          level: "info",
          message: `thought for ${(m.elapsedMs / 1000).toFixed(1)}s`,
        });
      }
    });

    client.on("assistant_start", () => {
      setSession((s) => ({...s, streaming: true, thinking: false}));
      setTimeline((prev) => {
        const item: TimelineItem = {
          kind: "assistant",
          id: nextId(),
          content: "",
          streaming: true,
          reasoning: "",
        };
        const next = appendCapped(prev, item);
        currentAssistantIdx.current = next.length - 1;
        return next;
      });
    });

    client.on("stream_delta", (m) => {
      setTimeline((prev) => {
        const idx = currentAssistantIdx.current;
        if (idx == null || idx >= prev.length) return prev;
        const item = prev[idx];
        if (item.kind !== "assistant") return prev;
        const copy = [...prev];
        copy[idx] = m.reasoning
          ? {...item, reasoning: item.reasoning + m.content}
          : {...item, content: item.content + m.content};
        return copy;
      });
    });

    client.on("assistant_end", (m) => {
      setTimeline((prev) => {
        const idx = currentAssistantIdx.current;
        if (idx == null || idx >= prev.length) return prev;
        const item = prev[idx];
        if (item.kind !== "assistant") return prev;
        const copy = [...prev];
        copy[idx] = {
          ...item,
          content: m.content || item.content,
          streaming: false,
        };
        return copy;
      });
      currentAssistantIdx.current = null;
      setSession((s) => ({...s, streaming: false}));
    });

    client.on("tool_call", (m) =>
      push({
        kind: "tool",
        id: m.id,
        name: m.name,
        category: m.category,
        args: m.args,
        risk: m.risk,
        ok: null,
        preview: null,
        error: null,
        fullAvailable: false,
      }),
    );

    client.on("tool_result", (m) => {
      setTimeline((prev) =>
        prev.map((it) =>
          it.kind === "tool" && it.id === m.id
            ? {
                ...it,
                ok: m.ok,
                preview: m.preview,
                error: m.error ?? null,
                fullAvailable: Boolean(m.fullAvailable),
              }
            : it,
        ),
      );
    });

    client.on("tool_approval_req", (m) =>
      push({
        kind: "approval",
        id: m.id,
        tool: m.tool,
        args: m.args,
        risk: m.risk,
        decided: "pending",
      }),
    );

    client.on("file_diff", (m) =>
      push({kind: "diff", id: nextId(), path: m.path, diff: m.diff}),
    );

    client.on("status", (m) =>
      setSession((s) => ({
        ...s,
        ctxUsed: m.ctxUsed,
        ctxLimit: m.ctxLimit,
        costUsd: m.costUsd,
        budgetUsd: m.budgetUsd,
        promptTokens: m.promptTokens,
        completionTokens: m.completionTokens,
        totalTokens: m.totalTokens,
      })),
    );

    client.on("agent_update", (m) =>
      setSession((s) => ({
        ...s,
        agents: {...s.agents, [m.agent.id]: m.agent},
      })),
    );

    client.on("agent_lifecycle", (m) =>
      setSession((s) => ({
        ...s,
        agents: {...s.agents, [m.agent.id]: m.agent},
      })),
    );

    client.on("model_changed", (m) =>
      setSession((s) => ({...s, model: m.model, provider: m.provider})),
    );

    client.on("auto_approve_changed", (m: {autoApprove: boolean}) =>
      setSession((s) => ({...s, autoApprove: Boolean(m.autoApprove)})),
    );

    client.on("reasoning_changed", (m: {effort: ReasoningEffort}) =>
      setSession((s) => ({...s, reasoning: m.effort ?? "none"})),
    );

    client.on("error", (m) =>
      push({
        kind: "error",
        id: nextId(),
        category: m.category,
        message: m.message,
        hint: m.hint,
        details: m.details,
      }),
    );

    client.on("info", (m) =>
      push({kind: "toast", id: nextId(), level: "info", message: m.message}),
    );
    client.on("warning", (m) =>
      push({kind: "toast", id: nextId(), level: "warning", message: m.message}),
    );
    client.on("success", (m) =>
      push({kind: "toast", id: nextId(), level: "success", message: m.message}),
    );

    client.on("goodbye", () => {
      goodbyeRef.current = true;
    });

    client.on("stderr", (chunk) => {
      stderrRef.current = (stderrRef.current + chunk).slice(-4000);
      setStderr(stderrRef.current);
    });

    client.on("exit", (info: {code: number | null; signal: string | null}) => {
      setSession((s) => ({...s, connected: false, ready: false}));

      const clean = goodbyeRef.current || info.code === 0;
      if (clean) return;

      const trimmed = stderrRef.current.trim();
      const preview =
        trimmed.length > 0
          ? trimmed.split("\n").slice(-20).join("\n")
          : "(no stderr output captured)";

      push({
        kind: "error",
        id: nextId(),
        category: "internal",
        message: readyRef.current
          ? `Agent process exited unexpectedly (code=${info.code ?? "?"}, signal=${info.signal ?? "none"}).`
          : `Agent failed to start (code=${info.code ?? "?"}, signal=${info.signal ?? "none"}).`,
        hint: readyRef.current
          ? "Restart `coderAI chat`. If the problem persists, check stderr below."
          : "Is Python installed and on PATH? Try `coderAI chat --python=$(which python3)`.",
        details: preview,
      });
    });

    client.start();

    return () => {
      void client.stop();
      clientRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.python, opts.cwd]);

  const showHelp = () => {
    push({
      kind: "toast",
      id: nextId(),
      level: "info",
      message: SLASH_HELP,
    });
  };

  const actions = useMemo<UseAgentResult["actions"]>(
    () => ({
      send: (text: string) => {
        const client = clientRef.current;
        if (!client) return;

        const trimmed = text.trim();
        if (!trimmed) return;

        // Echo the raw user input to the timeline so slash-commands still
        // produce a visible record of what was typed.
        setTimeline((prev) =>
          appendCapped(prev, {
            kind: "user",
            id: nextId(),
            text: trimmed,
          }),
        );

        if (trimmed.startsWith("/")) {
          handleSlashCommand(trimmed, client, push, nextId, showHelp);
          return;
        }

        client.sendMessage(trimmed);
      },
      cancel: () => clientRef.current?.cancel(),
      setModel: (m: string) => clientRef.current?.setModel(m),
      setReasoning: (e: ReasoningEffort) => clientRef.current?.setReasoning(e),
      toggleAutoApprove: () => clientRef.current?.toggleAutoApprove(),
      clearContext: () => {
        clientRef.current?.clearContext();
        setTimeline([]);
        currentAssistantIdx.current = null;
      },
      compactContext: () => clientRef.current?.compactContext(),
      getState: () => clientRef.current?.getState(),
      approveTool: (toolId: string, approve: boolean) => {
        clientRef.current?.approveTool(toolId, approve);
        setTimeline((prev) =>
          prev.map((it) =>
            it.kind === "approval" && it.id === toolId
              ? {...it, decided: approve ? "approved" : "denied"}
              : it,
          ),
        );
      },
      exit: () => clientRef.current?.exit(),
      showHelp,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return {session, timeline, actions, stderr};
}

function appendCapped(prev: TimelineItem[], item: TimelineItem): TimelineItem[] {
  if (prev.length < MAX_TIMELINE) return [...prev, item];
  const dropped = prev.length - TRIM_TO + 1;
  const marker: TimelineItem = {
    kind: "toast",
    id: `trim_${Date.now().toString(36)}`,
    level: "info",
    message: `… ${dropped} earlier timeline entries trimmed for performance …`,
  };
  return [marker, ...prev.slice(-TRIM_TO + 1), item];
}

// -- Slash commands ---------------------------------------------------------

const SLASH_HELP = [
  "Slash commands:",
  "  /help                  Show this help",
  "  /clear                 Wipe conversation & context",
  "  /compact               Summarize long context",
  "  /model [name]          Show or switch model (aliases: opus, sonnet, haiku)",
  "  /reasoning <effort>    Set thinking effort (none|low|medium|high)",
  "  /yolo | /auto-approve  Toggle auto-approve for high-risk tools",
  "  /status | /tokens      Re-emit session status",
  "  /agents                Show active agents (see table)",
  "  /exit | /quit          Shut down the agent",
].join("\n");

function handleSlashCommand(
  raw: string,
  client: AgentClient,
  push: (item: TimelineItem) => void,
  nextId: () => string,
  showHelp: () => void,
): void {
  const [head, ...rest] = raw.slice(1).split(/\s+/);
  const arg = rest.join(" ").trim();
  const cmd = head.toLowerCase();

  const toast = (
    level: "info" | "warning" | "success",
    message: string,
  ): void => push({kind: "toast", id: nextId(), level, message});

  switch (cmd) {
    case "help":
    case "?":
      showHelp();
      return;
    case "clear":
      client.clearContext();
      return;
    case "compact":
      client.compactContext();
      toast("info", "Compacting context…");
      return;
    case "model":
      if (!arg) {
        client.getState();
        toast("info", "Current model: (see status bar)");
      } else {
        client.setModel(arg);
        toast("info", `Switching to model ${arg}…`);
      }
      return;
    case "reasoning": {
      const normalized = arg.toLowerCase() as ReasoningEffort;
      if (!["high", "medium", "low", "none"].includes(normalized)) {
        toast(
          "warning",
          "Usage: /reasoning <high|medium|low|none>",
        );
        return;
      }
      client.setReasoning(normalized);
      toast("info", `Reasoning effort set to ${normalized}`);
      return;
    }
    case "yolo":
    case "auto-approve":
    case "autoapprove":
      client.toggleAutoApprove();
      return;
    case "tokens":
    case "status":
    case "context":
      client.getState();
      return;
    case "agents":
      toast("info", "Active agents shown in the table above.");
      return;
    case "exit":
    case "quit":
      client.exit();
      return;
    default:
      toast("warning", `Unknown command: /${head} — type /help for a list`);
  }
}
