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
  verbose: false,
  ctxUsed: 0,
  ctxLimit: 0,
  costUsd: 0,
  budgetUsd: 0,
  availableModels: null,
  agents: {},
  agentsFinishedAt: {},
  progress: null,
  sessionStartedAt: null,
};

export interface UseAgentResult {
  session: SessionState;
  timeline: TimelineItem[];
  /** When true, the interactive /help menu is open (prompt should be inactive). */
  helpMenuOpen: boolean;
  /** When true, the model picker overlay is open. */
  modelMenuOpen: boolean;
  /** When true, the reasoning effort picker is open. */
  reasoningMenuOpen: boolean;
  actions: {
    send: (text: string) => void;
    cancel: () => void;
    approveTool: (toolId: string, approve: boolean, always?: boolean) => void;
    exit: () => void;
    closeHelpMenu: () => void;
    closeModelMenu: () => void;
    closeReasoningMenu: () => void;
    toggleVerbose: () => void;
    revealReasoning: () => void;
  };
}

export function useAgent(opts: {python?: string; cwd?: string} = {}): UseAgentResult {
  const clientRef = useRef<AgentClient | null>(null);
  const [session, setSession] = useState<SessionState>(INITIAL);
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [helpMenuOpen, setHelpMenuOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [reasoningMenuOpen, setReasoningMenuOpen] = useState(false);

  // Mirror of the latest session state, kept on a ref so memoised actions can
  // read fresh values without re-creating their closures on every render. The
  // actions useMemo([]) is stable, so anything they read from `session` must
  // come from this ref instead.
  const sessionRef = useRef<SessionState>(INITIAL);
  sessionRef.current = session;

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
    setTimeline((prev) => appendCapped(prev, item, nextId));

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

  const actions = useMemo<UseAgentResult["actions"]>(
    () => {
      const showHelp = () => setHelpMenuOpen(true);
      const closeHelpMenu = () => setHelpMenuOpen(false);
      const closeModelMenu = () => setModelMenuOpen(false);
      const closeReasoningMenu = () => setReasoningMenuOpen(false);

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
        // Empty transcript is the primary signal, but a brief confirmation
        // helps a user who mis-typed verify the action landed.
        push({
          kind: "toast",
          id: nextId(),
          level: "success",
          message: "context cleared",
        });
      };

      const toggleVerbose = () => {
        setSession((s) => {
          const next = !s.verbose;
          // Tell the server to start (or stop) forwarding low-priority toasts
          // and chatty narration so the savings happen at the source, not
          // just in the renderer.
          clientRef.current?.setVerbosity(next ? "verbose" : "normal");
          push({
            kind: "toast",
            id: nextId(),
            level: "info",
            message: next ? "verbose: on" : "verbose: off",
          });
          return {...s, verbose: next};
        });
      };

      const revealReasoning = () => {
        // Find the latest assistant turn with non-empty reasoning and
        // surface it as an explicit timeline item the user opened.
        setTimeline((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            const it = prev[i];
            if (it.kind === "assistant" && it.reasoning.trim()) {
              return appendCapped(
                prev,
                {
                  kind: "toast",
                  id: nextId(),
                  level: "info",
                  message: `reasoning ↓\n${it.reasoning.trim()}`,
                },
                nextId,
              );
            }
          }
          return appendCapped(
            prev,
            {
              kind: "toast",
              id: nextId(),
              level: "info",
              message: "no reasoning captured for the last turn",
            },
            nextId,
          );
        });
      };

      const refreshAgents = () => {
        clientRef.current?.getState();
        push({
          kind: "toast",
          id: nextId(),
          level: "info",
          message: "agents panel refreshed",
        });
      };

      return {
        send: (text: string) => {
          const client = clientRef.current;
          if (!client) return;

          const trimmed = text.trim();
          if (!trimmed) return;

          // Echo the raw user input so slash-commands still produce a
          // visible record of what was typed.
          setTimeline((prev) =>
            appendCapped(
              prev,
              {kind: "user", id: nextId(), text: trimmed},
              nextId,
            ),
          );

          if (trimmed.startsWith("/")) {
            handleSlashCommand(trimmed, client, push, nextId, {
              showHelp,
              clearContext,
              toggleVerbose,
              revealReasoning,
              refreshAgents,
              showModelMenu: () => setModelMenuOpen(true),
              showReasoningMenu: () => setReasoningMenuOpen(true),
            });
            return;
          }

          client.sendMessage(trimmed);
        },
        cancel: () => clientRef.current?.cancel(),
        approveTool: (toolId: string, approve: boolean, always?: boolean) => {
          // "Allow always" must *enable* YOLO unconditionally — toggling
          // would silently turn it OFF if it was already on, which is the
          // opposite of what the dialog promises.
          if (approve && always && !sessionRef.current.autoApprove) {
            clientRef.current?.toggleAutoApprove();
          }
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
        closeModelMenu,
        closeReasoningMenu,
        toggleVerbose,
        revealReasoning,
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  return {session, timeline, helpMenuOpen, modelMenuOpen, reasoningMenuOpen, actions};
}

interface SlashHandlers {
  showHelp: () => void;
  clearContext: () => void;
  toggleVerbose: () => void;
  revealReasoning: () => void;
  refreshAgents: () => void;
  showModelMenu: () => void;
  showReasoningMenu: () => void;
}

function handleSlashCommand(
  raw: string,
  client: AgentClient,
  push: (item: TimelineItem) => void,
  nextId: () => string,
  handlers: SlashHandlers,
): void {
  const {showHelp, clearContext, toggleVerbose, revealReasoning, refreshAgents, showModelMenu, showReasoningMenu} =
    handlers;
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
      return;
    case "model":
    case "change-model":
    case "changemodel":
    case "switch-model": {
      // Unified model command:
      //   /model                 → show current + list
      //   /model <name>          → switch session model
      //   /model default <name>  → persist as default for new sessions
      const [first, ...tail] = arg ? arg.split(/\s+/) : [];
      if (!first) {
        client.listModels();
        showModelMenu();
        return;
      }
      if (first.toLowerCase() === "default") {
        const target = tail.join(" ").trim();
        if (!target) {
          toast("warning", "Usage: /model default <name>");
          return;
        }
        client.setDefaultModel(target);
        toast("success", `Default model set to ${target}`);
        return;
      }
      client.setModel(arg);
      toast("success", `Model set to ${arg}`);
      return;
    }
    case "reasoning":
    case "thinking": {
      if (!arg) {
        showReasoningMenu();
        return;
      }
      const normalized = arg.toLowerCase() as ReasoningEffort;
      if (!["high", "medium", "low", "none"].includes(normalized)) {
        toast(
          "warning",
          "Usage: /reasoning <high|medium|low|none>  (alias: /thinking)",
        );
        return;
      }
      client.setReasoning(normalized);
      toast("success", `Reasoning set to ${normalized}`);
      return;
    }
    case "yolo":
    case "auto-approve":
    case "autoapprove":
      client.toggleAutoApprove();
      return;
    case "allow-tool":
    case "disallow-tool":
    case "allowed-tools":
      client.sendMessage(raw);
      return;
    case "verbose":
      toggleVerbose();
      return;
    case "think":
    case "reveal":
      revealReasoning();
      return;
    case "tokens":
    case "status":
    case "context":
      client.getState();
      return;
    case "agents":
      // The panel updates live in the status region; the toast is the
      // only proof that the refresh actually fired.
      refreshAgents();
      return;
    case "show": {
      const topic = arg.toLowerCase();
      if (!topic) {
        toast(
          "warning",
          "Usage: /show <version|models|cost|info|config|system|tasks|plan>",
        );
        return;
      }
      if (topic === "plan") {
        client.getPlan();
        return;
      }
      client.reference(topic);
      return;
    }
    // Legacy aliases — silent backward compat, route through /show.
    case "version":
    case "providers":
    case "cost":
    case "pricing":
    case "system":
    case "diag":
    case "diagnostics":
    case "config":
    case "info":
    case "tasks":
    case "todos":
    case "task":
      client.reference(cmd === "providers" ? "models" : cmd);
      return;
    case "plan":
      client.getPlan();
      return;
    case "exit":
    case "quit":
      client.exit();
      return;
    default:
      toast(
        "warning",
        `Unknown command: /${head} · press / or type /help to open the menu`,
      );
  }
}
