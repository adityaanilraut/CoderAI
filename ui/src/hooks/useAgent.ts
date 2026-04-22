/**
 * React hook that owns the AgentClient lifecycle and exposes:
 *   - session state (model, context %, cost, agents)
 *   - the message timeline (user inputs, assistant turns, tool cards, errors)
 *   - imperative helpers (send, cancel, approveTool, ...)
 */

import {useEffect, useMemo, useRef, useState} from "react";
import {AgentClient} from "../rpc/agentClient.js";
import type {ReasoningEffort} from "../protocol.js";
import {
  attachAgentClientListeners,
  type StatusPayload,
} from "./agentClientListeners.js";
import {appendCapped} from "./timelineAppend.js";
import type {SessionState, TimelineItem} from "./agentStateTypes.js";

export type {SessionState, TimelineItem} from "./agentStateTypes.js";

const INITIAL: SessionState = {
  connected: false,
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
  agents: {},
};

export interface UseAgentResult {
  session: SessionState;
  timeline: TimelineItem[];
  /** When true, the interactive /help menu is open (prompt should be inactive). */
  helpMenuOpen: boolean;
  actions: {
    send: (text: string) => void;
    cancel: () => void;
    approveTool: (toolId: string, approve: boolean) => void;
    exit: () => void;
    closeHelpMenu: () => void;
  };
}

export function useAgent(opts: {python?: string; cwd?: string} = {}): UseAgentResult {
  const clientRef = useRef<AgentClient | null>(null);
  const [session, setSession] = useState<SessionState>(INITIAL);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
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
  const statusPendingRef = useRef<StatusPayload | null>(null);
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

    const detach = attachAgentClientListeners(
      client,
      {setSession, setTimeline, nextId, push},
      {
        readyRef,
        goodbyeRef,
        stderrRef,
        currentAssistantId,
        streamPendingContentRef,
        streamPendingReasoningRef,
        streamFlushTimerRef,
        statusPendingRef,
        statusThrottleTimerRef,
      },
    );

    client.start();

    return () => {
      resetStreamFlushState();
      if (statusThrottleTimerRef.current !== null) {
        clearTimeout(statusThrottleTimerRef.current);
        statusThrottleTimerRef.current = null;
      }
      detach();
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
      closeHelpMenu,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return {session, timeline, helpMenuOpen, actions};
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
