/**
 * Wires `AgentClient` IPC events to React state. Extracted from `useAgent` for
 * easier unit testing and on-call debugging of event ordering.
 */

import type {Dispatch, SetStateAction, MutableRefObject} from "react";
import {AgentClient} from "../rpc/agentClient.js";
import type {AgentEvent, AgentInfo} from "../protocol.js";
import type {SessionState, TimelineItem} from "./agentStateTypes.js";
import {appendCapped} from "./timelineAppend.js";

/** Coalesce stream_delta IPC into fewer Ink redraws (~8fps cap).
 *  A higher value reduces ANSI cursor thrash that causes scroll-to-top
 *  issues in terminals that don't handle rapid live-region redraws well. */
const STREAM_FLUSH_MS = 120;
/** Cap status bar updates while tokens/context churn. */
const STATUS_THROTTLE_MS = 250;

export type StatusPayload = {
  ctxUsed: number;
  ctxLimit: number;
  costUsd: number;
  budgetUsd: number;
};

export interface AgentListenerRefs {
  readyRef: MutableRefObject<boolean>;
  goodbyeRef: MutableRefObject<boolean>;
  stderrRef: MutableRefObject<string>;
  currentAssistantId: MutableRefObject<string | null>;
  streamPendingContentRef: MutableRefObject<string>;
  streamPendingReasoningRef: MutableRefObject<string>;
  streamFlushTimerRef: MutableRefObject<ReturnType<typeof setTimeout> | null>;
  statusPendingRef: MutableRefObject<StatusPayload | null>;
  statusThrottleTimerRef: MutableRefObject<ReturnType<typeof setTimeout> | null>;
}

export interface AgentListenerDispatch {
  setSession: Dispatch<SetStateAction<SessionState>>;
  setTimeline: Dispatch<SetStateAction<TimelineItem[]>>;
  nextId: () => string;
  push: (item: TimelineItem) => void;
}

/**
 * Subscribes to all agent events. Returns a disposer that removes listeners and
 * should be called on unmount (in addition to `client.stop()`).
 */
export function attachAgentClientListeners(
  client: AgentClient,
  dispatch: AgentListenerDispatch,
  refs: AgentListenerRefs,
): () => void {
  const {setSession, setTimeline, nextId, push} = dispatch;
  const {
    readyRef,
    goodbyeRef,
    stderrRef,
    currentAssistantId,
    streamPendingContentRef,
    streamPendingReasoningRef,
    streamFlushTimerRef,
    statusPendingRef,
    statusThrottleTimerRef,
  } = refs;

  type Ev = AgentEvent["event"] | "stderr" | "exit";
  const removeFns: Array<() => void> = [];
  const add = (ev: Ev, fn: (payload: any) => void) => {
    client.on(ev, fn);
    removeFns.push(() => {
      client.removeListener(ev, fn);
    });
  };

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

  const recoverIncompleteTurn = () => {
    resetStreamFlushState();
    currentAssistantId.current = null;
    setSession((s) => ({...s, streaming: false, thinking: false}));
    setTimeline((prev) =>
      prev.map((it) =>
        it.kind === "assistant" && it.streaming ? {...it, streaming: false} : it,
      ),
    );
  };

  const applyStatusPayload = (m: StatusPayload) => {
    setSession((s) => ({
      ...s,
      ctxUsed: m.ctxUsed,
      ctxLimit: m.ctxLimit,
      costUsd: m.costUsd,
      budgetUsd: m.budgetUsd,
    }));
  };

  const FINISHED_AGENT_STATUSES = new Set(["done", "error", "cancelled"]);

  const upsertAgentInSession = (agent: AgentInfo) => {
    setSession((s) => {
      const prev = s.agents[agent.id];
      const becameFinished =
        FINISHED_AGENT_STATUSES.has(agent.status) &&
        (!prev || !FINISHED_AGENT_STATUSES.has(prev.status));
      const finishedAt = {...s.agentsFinishedAt};
      if (becameFinished) {
        finishedAt[agent.id] = Date.now();
      } else if (!FINISHED_AGENT_STATUSES.has(agent.status)) {
        // Agent transitioned back into a live state — clear stale cull mark.
        delete finishedAt[agent.id];
      }
      return {
        ...s,
        agents: {...s.agents, [agent.id]: agent},
        agentsFinishedAt: finishedAt,
      };
    });
  };

  add("hello", (m: Extract<AgentEvent, {event: "hello"}>) => {
    setSession((s) => ({
      ...s,
      connected: true,
      model: m.model,
      provider: m.provider,
      cwd: m.cwd,
      version: m.version,
      ctxLimit: m.contextLimit,
      budgetUsd: m.budgetLimit,
      autoApprove: m.autoApprove,
    }));
  });

  add("ready", () => {
    readyRef.current = true;
    recoverIncompleteTurn();
  });

  // Tracks whether the current turn is still in the "waiting for first
  // token" window. Set on phase=start, cleared on the first text/reasoning
  // delta — that transition is what flips the UI from "thinking" to
  // "streaming". Lives in closure scope rather than as a ref because it
  // never needs to be observed from outside this listener.
  let awaitingFirstDelta = false;
  const markFirstDeltaIfPending = () => {
    if (!awaitingFirstDelta) return;
    awaitingFirstDelta = false;
    setSession((s) => ({...s, thinking: false, streaming: true}));
  };

  add("turn", (m: Extract<AgentEvent, {event: "turn"}>) => {
    if (m.phase === "start") {
      resetStreamFlushState();
      // Until the first token arrives the LLM is still "thinking" —
      // streaming flips on only when we see a text/reasoning delta.
      awaitingFirstDelta = true;
      setSession((s) => ({...s, thinking: true, streaming: false}));
      setTimeline((prev) => {
        const item: TimelineItem = {
          kind: "assistant",
          id: nextId(),
          content: "",
          streaming: true,
          reasoning: "",
        };
        const next = appendCapped(prev, item, nextId);
        currentAssistantId.current = item.id;
        return next;
      });
    } else if (m.phase === "reasoning" && m.delta) {
      markFirstDeltaIfPending();
      streamPendingReasoningRef.current += m.delta;
      scheduleStreamFlush();
    } else if (m.phase === "text" && m.delta) {
      markFirstDeltaIfPending();
      streamPendingContentRef.current += m.delta;
      scheduleStreamFlush();
    } else if (m.phase === "end") {
      awaitingFirstDelta = false;
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
            if (!mergedContent.trim() && !mergedReasoning.trim()) {
              return prev.filter((_, idx) => idx !== i);
            }
            const copy = [...prev];
            copy[i] = {
              ...item,
              content: mergedContent,
              reasoning: mergedReasoning,
              streaming: false,
            };
            return copy;
          }
        }
        return prev;
      });
      currentAssistantId.current = null;
      setSession((s) => ({...s, streaming: false, thinking: false}));
    }
  });

  add("tool", (m: Extract<AgentEvent, {event: "tool"}>) => {
    if (m.phase === "queued" || m.phase === "running") {
      setTimeline((prev) => {
        const exists = prev.some((it) => it.kind === "tool" && it.id === m.id);
        if (!exists && m.payload.name) {
          return appendCapped(
            prev,
            {
              kind: "tool",
              id: m.id,
              name: m.payload.name,
              category: m.payload.category || "other",
              args: m.payload.args || {},
              risk: m.payload.risk || "low",
              ok: null,
              preview: null,
              error: null,
              fullAvailable: false,
            },
            nextId,
          );
        }
        return prev;
      });
    } else if (m.phase === "ok" || m.phase === "err") {
      setTimeline((prev) => {
        const idx = prev.findIndex((it) => it.kind === "tool" && it.id === m.id);
        if (idx === -1) return prev;
        const item = prev[idx];
        if (item.kind !== "tool") return prev;
        const copy = [...prev];
        copy[idx] = {
          ...item,
          ok: m.phase === "ok",
          preview: m.payload.preview ?? "",
          error: m.payload.error ?? null,
          fullAvailable: Boolean(m.payload.fullAvailable),
        };
        return copy;
      });
    } else if (m.phase === "awaiting_approval") {
      push({
        kind: "approval",
        id: m.id,
        tool: m.payload.name,
        args: m.payload.args || {},
        risk: m.payload.risk || "low",
        decided: "pending",
      });
    }
  });

  add("agent", (m: Extract<AgentEvent, {event: "agent"}>) => {
    upsertAgentInSession(m.info);
  });

  add("session_patch", (m: Extract<AgentEvent, {event: "session_patch"}>) => {
    setSession((s) => ({
      ...s,
      ...(m.model !== undefined ? {model: m.model} : {}),
      ...(m.provider !== undefined ? {provider: m.provider} : {}),
      ...(m.autoApprove !== undefined ? {autoApprove: m.autoApprove} : {}),
      ...(m.reasoning !== undefined ? {reasoning: m.reasoning} : {}),
    }));
  });

  add("file_diff", (m: Extract<AgentEvent, {event: "file_diff"}>) =>
    push({kind: "diff", id: nextId(), path: m.path, diff: m.diff}),
  );

  // Server-side toasts. The Python agent emits these for slash-command
  // replies (`/show <topic>`, `/plan`), state-change confirmations
  // (`Cancelled agent`), and per-command warnings (`Unknown command:`). They
  // were previously silently dropped — every visible result of `/show ...`
  // depends on these landing on the timeline.
  const pushToast = (level: "info" | "warning" | "success", message: string) =>
    push({kind: "toast", id: nextId(), level, message});

  add("info", (m: Extract<AgentEvent, {event: "info"}>) =>
    pushToast("info", m.message),
  );
  add("warning", (m: Extract<AgentEvent, {event: "warning"}>) =>
    pushToast("warning", m.message),
  );
  add("success", (m: Extract<AgentEvent, {event: "success"}>) =>
    pushToast("success", m.message),
  );

  add("status", (m: Extract<AgentEvent, {event: "status"}>) => {
    const payload: StatusPayload = {
      ctxUsed: m.ctxUsed,
      ctxLimit: m.ctxLimit,
      costUsd: m.costUsd,
      budgetUsd: m.budgetUsd,
    };
    statusPendingRef.current = payload;
    if (statusThrottleTimerRef.current !== null) return;
    applyStatusPayload(payload);
    statusThrottleTimerRef.current = setTimeout(() => {
      statusThrottleTimerRef.current = null;
      const p = statusPendingRef.current;
      if (p) applyStatusPayload(p);
    }, STATUS_THROTTLE_MS);
  });

  add("error", (m: Extract<AgentEvent, {event: "error"}>) => {
    recoverIncompleteTurn();
    push({
      kind: "error",
      id: nextId(),
      category: m.category,
      message: m.message,
      hint: m.hint,
      details: m.details,
    });
  });

  add("goodbye", () => {
    recoverIncompleteTurn();
    goodbyeRef.current = true;
  });

  add("stderr", (chunk: string) => {
    stderrRef.current = (stderrRef.current + chunk).slice(-4000);
  });

  add("exit", (info: {code: number | null; signal: string | null}) => {
    recoverIncompleteTurn();
    setSession((s) => ({...s, connected: false}));

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

  return () => {
    for (const off of removeFns) off();
  };
}
