/**
 * Wires `AgentClient` IPC events to React state. Extracted from `useAgent` for
 * easier unit testing and on-call debugging of event ordering.
 */

import type {Dispatch, SetStateAction, MutableRefObject} from "react";
import {AgentClient} from "../rpc/agentClient.js";
import type {AgentEvent, AgentInfo} from "../protocol.js";
import type {SessionState, TimelineItem} from "./agentStateTypes.js";
import {appendCapped} from "./timelineAppend.js";

/** Coalesce stream_delta IPC into fewer Ink redraws (~40fps cap). */
const STREAM_FLUSH_MS = 24;
/** Cap status bar updates while tokens/context churn. */
const STATUS_THROTTLE_MS = 100;

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

  const mergeAgentIntoTimeline = (agent: AgentInfo) => {
    setTimeline((prev) => {
      for (let i = prev.length - 1; i >= 0; i--) {
        const it = prev[i];
        if (it.kind === "agent" && it.agent.id === agent.id) {
          const copy = [...prev];
          copy[i] = {...it, agent};
          return copy;
        }
      }
      return appendCapped(prev, {
        kind: "agent",
        id: `agent_${agent.id}`,
        agent,
      });
    });
  };

  add("hello", (m: any) => {
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

  add("ready", () => {
    readyRef.current = true;
    recoverIncompleteTurn();
  });

  add("thinking_start", () =>
    setSession((s) => ({...s, thinking: true})),
  );

  add("thinking_end", (m: any) => {
    setSession((s) => ({...s, thinking: false}));
    if (typeof m.elapsedMs === "number" && m.elapsedMs >= 2000) {
      push({
        kind: "toast",
        id: nextId(),
        level: "info",
        message: `thought for ${(m.elapsedMs / 1000).toFixed(1)}s`,
      });
    }
  });

  add("assistant_start", () => {
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

  add("stream_delta", (m: any) => {
    if (m.reasoning) {
      streamPendingReasoningRef.current += m.content;
    } else {
      streamPendingContentRef.current += m.content;
    }
    scheduleStreamFlush();
  });

  add("assistant_end", (m: any) => {
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
          const finalContent =
            mergedContent.trim().length > 0
              ? mergedContent
              : typeof m.content === "string"
                ? m.content
                : "";
          if (!finalContent.trim() && !mergedReasoning.trim()) {
            return prev.filter((_, idx) => idx !== i);
          }
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

  add("tool_call", (m: any) =>
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

  add("tool_result", (m: any) => {
    setTimeline((prev) => {
      const idx = prev.findIndex((it) => it.kind === "tool" && it.id === m.id);
      if (idx === -1) return prev;
      const item = prev[idx];
      if (item.kind !== "tool") return prev;
      const copy = [...prev];
      copy[idx] = {
        ...item,
        ok: m.ok,
        preview: m.preview,
        error: m.error ?? null,
        fullAvailable: Boolean(m.fullAvailable),
      };
      return copy;
    });
  });

  add("tool_approval_req", (m: any) =>
    push({
      kind: "approval",
      id: m.id,
      tool: m.tool,
      args: m.args,
      risk: m.risk,
      decided: "pending",
    }),
  );

  add("file_diff", (m: any) =>
    push({kind: "diff", id: nextId(), path: m.path, diff: m.diff}),
  );

  add("status", (m: any) => {
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

  add("agent_update", (m: any) => {
    setSession((s) => ({
      ...s,
      agents: {...s.agents, [m.agent.id]: m.agent},
    }));
    mergeAgentIntoTimeline(m.agent);
  });

  add("agent_lifecycle", (m: any) => {
    setSession((s) => ({
      ...s,
      agents: {...s.agents, [m.agent.id]: m.agent},
    }));
    mergeAgentIntoTimeline(m.agent);
  });

  add("model_changed", (m: any) =>
    setSession((s) => ({...s, model: m.model, provider: m.provider})),
  );

  add("auto_approve_changed", (m: any) =>
    setSession((s) => ({...s, autoApprove: Boolean(m.autoApprove)})),
  );

  add("reasoning_changed", (m: any) =>
    setSession((s) => ({...s, reasoning: m.effort ?? "none"})),
  );

  add("error", (m: any) => {
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

  add("info", (m: any) =>
    push({kind: "toast", id: nextId(), level: "info", message: m.message}),
  );
  add("warning", (m: any) =>
    push({kind: "toast", id: nextId(), level: "warning", message: m.message}),
  );
  add("success", (m: any) =>
    push({kind: "toast", id: nextId(), level: "success", message: m.message}),
  );

  add("goodbye", () => {
    recoverIncompleteTurn();
    goodbyeRef.current = true;
  });

  add("stderr", (chunk: any) => {
    stderrRef.current = (stderrRef.current + chunk).slice(-4000);
  });

  add("exit", (info: any) => {
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
