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

/** Coalesce stream_delta IPC into fewer Ink redraws (~40fps cap). */
const STREAM_FLUSH_MS = 24;
/** Cap status bar updates while tokens/context churn. */
const STATUS_THROTTLE_MS = 100;

export interface UseAgentResult {
  session: SessionState;
  timeline: TimelineItem[];
  stderr: string;
  /** When true, the interactive /help menu is open (prompt should be inactive). */
  helpMenuOpen: boolean;
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
    closeHelpMenu: () => void;
  };
}

export function useAgent(opts: {python?: string; cwd?: string} = {}): UseAgentResult {
  const clientRef = useRef<AgentClient | null>(null);
  const [session, setSession] = useState<SessionState>(INITIAL);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [stderr, setStderr] = useState<string>("");
  const [helpMenuOpen, setHelpMenuOpen] = useState(false);

  // Refs to avoid closure staleness inside event handlers.
  const readyRef = useRef(false);
  const goodbyeRef = useRef(false);
  const stderrRef = useRef("");
  // Tracks the id of the currently-streaming assistant item so
  // stream_delta doesn't have to scan the whole timeline (just the end).
  const currentAssistantId = useRef<string | null>(null);
  const streamPendingContentRef = useRef("");
  const streamPendingReasoningRef = useRef("");
  const streamFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const statusPendingRef = useRef<{
    ctxUsed: number;
    ctxLimit: number;
    costUsd: number;
    budgetUsd: number;
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  } | null>(null);
  const statusThrottleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  // Monotonic id generator shared across event handlers AND the synchronous
  // actions below. Kept on a ref so it survives re-renders.
  const uidRef = useRef(0);
  const nextId = () => `i_${++uidRef.current}_${Date.now().toString(36)}`;

  const push = (item: TimelineItem) =>
    setTimeline((prev) => appendCapped(prev, item));

  useEffect(() => {
    const client = new AgentClient({python: opts.python, cwd: opts.cwd});
    clientRef.current = client;

    const resetStreamFlushState = () => {
      streamPendingContentRef.current = "";
      streamPendingReasoningRef.current = "";
      if (streamFlushTimerRef.current !== null) {
        clearTimeout(streamFlushTimerRef.current);
        streamFlushTimerRef.current = null;
      }
    };

    const flushStreamBuffers = () => {
      const addC = streamPendingContentRef.current;
      const addR = streamPendingReasoningRef.current;
      if (!addC && !addR) return;
      streamPendingContentRef.current = "";
      streamPendingReasoningRef.current = "";
      const id = currentAssistantId.current;
      if (!id) return;
      setTimeline((prev) => {
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].id === id && prev[i].kind === "assistant") {
            const item = prev[i] as Extract<TimelineItem, {kind: "assistant"}>;
            const copy = [...prev];
            copy[i] = {
              ...item,
              content: item.content + addC,
              reasoning: item.reasoning + addR,
            };
            return copy;
          }
        }
        return prev;
      });
    };

    const scheduleStreamFlush = () => {
      if (streamFlushTimerRef.current !== null) return;
      streamFlushTimerRef.current = setTimeout(() => {
        streamFlushTimerRef.current = null;
        flushStreamBuffers();
      }, STREAM_FLUSH_MS);
    };

    const applyStatusPayload = (m: {
      ctxUsed: number;
      ctxLimit: number;
      costUsd: number;
      budgetUsd: number;
      promptTokens: number;
      completionTokens: number;
      totalTokens: number;
    }) => {
      setSession((s) => ({
        ...s,
        ctxUsed: m.ctxUsed,
        ctxLimit: m.ctxLimit,
        costUsd: m.costUsd,
        budgetUsd: m.budgetUsd,
        promptTokens: m.promptTokens,
        completionTokens: m.completionTokens,
        totalTokens: m.totalTokens,
      }));
    };

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
      resetStreamFlushState();
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
        currentAssistantId.current = item.id;
        return next;
      });
    });

    client.on("stream_delta", (m) => {
      if (m.reasoning) {
        streamPendingReasoningRef.current += m.content;
      } else {
        streamPendingContentRef.current += m.content;
      }
      scheduleStreamFlush();
    });

    client.on("assistant_end", (m) => {
      if (streamFlushTimerRef.current !== null) {
        clearTimeout(streamFlushTimerRef.current);
        streamFlushTimerRef.current = null;
      }
      const pendingC = streamPendingContentRef.current;
      const pendingR = streamPendingReasoningRef.current;
      streamPendingContentRef.current = "";
      streamPendingReasoningRef.current = "";

      setTimeline((prev) => {
        const id = currentAssistantId.current;
        if (!id) return prev;
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].id === id && prev[i].kind === "assistant") {
            const item = prev[i] as Extract<TimelineItem, {kind: "assistant"}>;
            const mergedContent = item.content + pendingC;
            const mergedReasoning = item.reasoning + pendingR;
            const fromServer = (m.content ?? "").trim();
            // Prefer the server's final string when non-empty: after tool loops
            // it carries every assistant turn joined (see agent_loop). Fall
            // back to merged stream buffers if the server sent nothing.
            const finalContent =
              fromServer.length > 0 ? m.content : mergedContent;
            const copy = [...prev];
            copy[i] = {
              ...item,
              content: finalContent,
              reasoning: mergedReasoning,
              streaming: false,
            };
            return copy;
          }
        }
        return prev;
      });
      currentAssistantId.current = null;
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

    client.on("status", (m) => {
      statusPendingRef.current = {
        ctxUsed: m.ctxUsed,
        ctxLimit: m.ctxLimit,
        costUsd: m.costUsd,
        budgetUsd: m.budgetUsd,
        promptTokens: m.promptTokens,
        completionTokens: m.completionTokens,
        totalTokens: m.totalTokens,
      };
      if (statusThrottleTimerRef.current !== null) return;
      applyStatusPayload(m);
      statusThrottleTimerRef.current = setTimeout(() => {
        statusThrottleTimerRef.current = null;
        const p = statusPendingRef.current;
        if (p) applyStatusPayload(p);
      }, STATUS_THROTTLE_MS);
    });

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
      resetStreamFlushState();
      if (statusThrottleTimerRef.current !== null) {
        clearTimeout(statusThrottleTimerRef.current);
        statusThrottleTimerRef.current = null;
      }
      void client.stop();
      clientRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opts.python, opts.cwd]);

  const showHelp = () => setHelpMenuOpen(true);
  const closeHelpMenu = () => setHelpMenuOpen(false);

  const clearContext = () => {
    setHelpMenuOpen(false);
    if (streamFlushTimerRef.current !== null) {
      clearTimeout(streamFlushTimerRef.current);
      streamFlushTimerRef.current = null;
    }
    streamPendingContentRef.current = "";
    streamPendingReasoningRef.current = "";
    clientRef.current?.clearContext();
    setTimeline([]);
    currentAssistantId.current = null;
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
          handleSlashCommand(
            trimmed,
            client,
            push,
            nextId,
            showHelp,
            clearContext,
          );
          return;
        }

        client.sendMessage(trimmed);
      },
      cancel: () => clientRef.current?.cancel(),
      setModel: (m: string) => clientRef.current?.setModel(m),
      setReasoning: (e: ReasoningEffort) => clientRef.current?.setReasoning(e),
      toggleAutoApprove: () => clientRef.current?.toggleAutoApprove(),
      clearContext,
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
      closeHelpMenu,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return {session, timeline, stderr, helpMenuOpen, actions};
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

function handleSlashCommand(
  raw: string,
  client: AgentClient,
  push: (item: TimelineItem) => void,
  nextId: () => string,
  showHelp: () => void,
  clearContext: () => void,
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
      clearContext();
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
    case "change-model":
    case "changemodel":
    case "switch-model":
      if (arg) {
        client.setModel(arg);
        toast("info", `Switching to model ${arg}…`);
      } else {
        client.getState();
        client.reference("models");
        toast(
          "info",
          "To switch: type /model <name> or /change-model <name> (names listed above)",
        );
      }
      return;
    case "reasoning":
    case "thinking": {
      const normalized = arg.toLowerCase() as ReasoningEffort;
      if (!["high", "medium", "low", "none"].includes(normalized)) {
        toast(
          "warning",
          "Usage: /reasoning <high|medium|low|none>  (alias: /thinking)",
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
      toast(
        "info",
        "Agents table (above): main + sub-agents from delegate_task. Rows update live; /status refreshes session bar.",
      );
      return;
    case "version":
    case "v":
      client.reference("version");
      return;
    case "models":
    case "providers":
      client.reference("models");
      return;
    case "cost":
    case "pricing":
      client.reference("cost");
      return;
    case "system":
    case "diag":
    case "diagnostics":
      client.reference("system");
      return;
    case "config":
      client.reference("config");
      return;
    case "info":
      client.reference("info");
      return;
    case "tasks":
    case "todos":
    case "task":
      client.reference("tasks");
      return;
    case "default":
      if (!arg) {
        toast(
          "warning",
          "Usage: /default <model> — saves default for new sessions (see /models)",
        );
        return;
      }
      client.setDefaultModel(arg);
      return;
    case "plan":
      client.getPlan();
      return;
    case "exit":
    case "quit":
      client.exit();
      return;
    default:
      toast("warning", `Unknown command: /${head} — type /help for a list`);
  }
}
