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
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import {BEL, ESC} from "../lib/hyperlink.js";

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
  promptTokens: 0,
  completionTokens: 0,
  availableModels: null,
  availablePersonas: null,
  availableSkills: null,
  contextFiles: null,
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
  /** When true, the transcript search overlay is open. */
  searchOpen: boolean;
  /** When true, the context files overlay is open. */
  contextOpen: boolean;
  /** When true, the persona menu is open. */
  personaMenuOpen: boolean;
  /** When true, the skills menu is open. */
  skillsMenuOpen: boolean;
  /** Current search filter string for the transcript search overlay. */
  searchFilter: string;
  /** Timeline index to highlight after a search jump (null when inactive). */
  highlightTimelineIndex: number | null;
  /**
   * Single source of truth for "exit is armed — next ^C or /exit confirms".
   * Both the Ctrl+C handler in App.tsx and the /exit slash handler check
   * and toggle this same flag, so the two routes can't disagree.
   */
  exitArmed: boolean;
  /**
   * Remove a finished-agent entry from the agentsFinishedAt map. Called by
   * AgentTree once the grace window has elapsed so the map doesn't grow
   * unboundedly across a long session.
   */
  gcFinishedAgent: (id: string) => void;
  actions: {
    send: (text: string) => void;
    cancel: () => void;
    approveTool: (toolId: string, approve: boolean, always?: boolean) => void;
    exit: () => void;
    /** Arm the exit-confirmation window (5s). */
    armExit: () => void;
    /** If armed, finalize exit; otherwise arm. Returns true iff exit was sent. */
    confirmExit: () => boolean;
    closeHelpMenu: () => void;
    closeModelMenu: () => void;
    closeReasoningMenu: () => void;
    closeSearch: () => void;
    closeContext: () => void;
    closePersonaMenu: () => void;
    closeSkillsMenu: () => void;
    setSearchFilter: (f: string) => void;
    jumpToTimelineIndex: (index: number) => void;
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
  const [searchOpen, setSearchOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [personaMenuOpen, setPersonaMenuOpen] = useState(false);
  const [skillsMenuOpen, setSkillsMenuOpen] = useState(false);
  const [searchFilter, setSearchFilter] = useState("");
  const [highlightTimelineIndex, setHighlightTimelineIndex] = useState<number | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Reactive armed flag — used to render the prompt's "^C again to exit" hint
  // AND consulted by the /exit handler so both confirmation routes share state.
  const [exitArmed, setExitArmed] = useState(false);
  const exitArmedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Mirror of `exitArmed` for the memoized actions object to read without
  // being re-created on every armed-state flip.
  const exitArmedRefMirror = useRef(false);
  exitArmedRefMirror.current = exitArmed;

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
  const timelineRef = useRef<TimelineItem[]>([]);
  timelineRef.current = timeline;

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
      const closeSearch = () => { setSearchOpen(false); setSearchFilter(""); };
      const closeContext = () => setContextOpen(false);
      const closePersonaMenu = () => setPersonaMenuOpen(false);
      const closeSkillsMenu = () => setSkillsMenuOpen(false);
      const setSearchFilterAction = (f: string) => setSearchFilter(f);

      const armExit = () => {
        if (exitArmedTimerRef.current) clearTimeout(exitArmedTimerRef.current);
        setExitArmed(true);
        exitArmedTimerRef.current = setTimeout(() => {
          setExitArmed(false);
          exitArmedTimerRef.current = null;
        }, 5000);
      };

      const confirmExit = (): boolean => {
        // If the window is still open, finalize. Otherwise arm and toast,
        // mirroring the /exit flow so Ctrl+C and /exit can't disagree about
        // whether shutdown has been requested.
        if (exitArmedTimerRef.current) clearTimeout(exitArmedTimerRef.current);
        exitArmedTimerRef.current = null;
        if (exitArmedRefMirror.current) {
          setExitArmed(false);
          clientRef.current?.exit();
          return true;
        }
        armExit();
        return false;
      };

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
        setHighlightTimelineIndex(null);
        if (highlightTimerRef.current !== null) {
          clearTimeout(highlightTimerRef.current);
          highlightTimerRef.current = null;
        }
        setSession((s) => ({
          ...s,
          agents: {},
          agentsFinishedAt: {},
        }));
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

      const jumpToTimelineIndex = (index: number) => {
        closeSearch();
        setHighlightTimelineIndex(index);
        if (highlightTimerRef.current !== null) {
          clearTimeout(highlightTimerRef.current);
        }
        highlightTimerRef.current = setTimeout(() => {
          setHighlightTimelineIndex(null);
          highlightTimerRef.current = null;
        }, 8000);
        const item = timelineRef.current[index];
        if (item) {
          push({
            kind: "toast",
            id: nextId(),
            level: "info",
            message: `jumped to match ↓\n${summarizeTimelineItem(item)}`,
          });
        }
      };

      return {
        send: (text: string) => {
          const client = clientRef.current;
          if (!client) return;

          setHighlightTimelineIndex(null);
          if (highlightTimerRef.current !== null) {
            clearTimeout(highlightTimerRef.current);
            highlightTimerRef.current = null;
          }

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
              showSearch: () => { setSearchOpen(true); setSearchFilter(""); },
              showContext: () => setContextOpen(true),
              showPersonaMenu: () => setPersonaMenuOpen(true),
              showSkillsMenu: () => setSkillsMenuOpen(true),
              closeSearch,
              setSearchFilterAction,
              confirmExit,
            }, {
              timelineRef,
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
        armExit,
        confirmExit,
        closeHelpMenu,
        closeModelMenu,
        closeReasoningMenu,
        closeSearch,
        closeContext,
        closePersonaMenu,
        closeSkillsMenu,
        setSearchFilter: setSearchFilterAction,
        jumpToTimelineIndex,
        toggleVerbose,
        revealReasoning,
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const gcFinishedAgent = (id: string) => {
    setSession((s) => {
      if (!(id in s.agentsFinishedAt)) return s;
      const next = {...s.agentsFinishedAt};
      delete next[id];
      return {...s, agentsFinishedAt: next};
    });
  };

  return {session, timeline, helpMenuOpen, modelMenuOpen, reasoningMenuOpen, searchOpen, contextOpen, personaMenuOpen, skillsMenuOpen, searchFilter, highlightTimelineIndex, exitArmed, gcFinishedAgent, actions};
}

interface SlashHandlers {
  showHelp: () => void;
  clearContext: () => void;
  toggleVerbose: () => void;
  revealReasoning: () => void;
  refreshAgents: () => void;
  showModelMenu: () => void;
  showReasoningMenu: () => void;
  showSearch: () => void;
  showContext: () => void;
  showPersonaMenu: () => void;
  showSkillsMenu: () => void;
  closeSearch: () => void;
  setSearchFilterAction: (f: string) => void;
  /** Returns true iff the exit was finalized; false means "armed". */
  confirmExit: () => boolean;
}

interface SlashRefs {
  timelineRef: React.MutableRefObject<TimelineItem[]>;
}

function handleSlashCommand(
  raw: string,
  client: AgentClient,
  push: (item: TimelineItem) => void,
  nextId: () => string,
  handlers: SlashHandlers,
  refs: SlashRefs,
): void {
  const {showHelp, clearContext, toggleVerbose, revealReasoning, refreshAgents, showModelMenu, showReasoningMenu, showSearch, showContext, showPersonaMenu, showSkillsMenu, closeSearch, setSearchFilterAction, confirmExit} =
    handlers;
  const {timelineRef} = refs;
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
      toast("info", "compacting context…");
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
    case "persona":
      if (!arg || arg === "list") {
        client.listPersonas();
        showPersonaMenu();
      } else {
        client.sendMessage(raw);
      }
      return;
    case "skills":
      if (!arg || arg === "list") {
        client.listSkills();
        showSkillsMenu();
      } else {
        client.sendMessage(raw);
      }
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
      showContext();
      return;
    case "code-search":
    case "search-code":
    case "cs":
      if (!arg) {
        toast("warning", "Usage: /code-search <query>");
      } else {
        client.searchCodebase(arg);
      }
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
      if (!confirmExit()) {
        toast("warning", "Type /exit (or press ^C) again to confirm shutdown (resets in 5s)");
      }
      return;
    case "export":
    case "save": {
      const defaultName = `coderAI-session-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}.md`;
      const target = arg || path.join(os.homedir(), "Desktop", defaultName);
      const content = timelineToMarkdown(timelineRef.current);
      try {
        fs.mkdirSync(path.dirname(target), {recursive: true});
        fs.writeFileSync(target, content, "utf8");
        toast("success", `Exported to ${target}`);
      } catch (e: any) {
        toast("warning", `Export failed: ${e.message}`);
      }
      return;
    }
    case "search":
    case "find":
      if (!arg) {
        showSearch();
      } else {
        setSearchFilterAction(arg);
        showSearch();
      }
      return;
    case "copy": {
      const lastAsst = findLastAssistant(timelineRef.current);
      if (!lastAsst) {
        toast("warning", "No assistant response to copy");
      } else {
        copyToClipboard(lastAsst);
        // OSC-52 is fire-and-forget: terminals that don't honour it just
        // discard the escape sequence. Phrase the toast so the user knows
        // the clipboard *might* not have updated on every terminal.
        toast(
          "info",
          `Sent ${lastAsst.length.toLocaleString()} chars via OSC-52 — paste to verify (requires iTerm2/kitty/WezTerm/etc.)`,
        );
      }
      return;
    }
    case "theme": {
      const themeName = arg.toLowerCase();
      if (themeName !== "dark" && themeName !== "light") {
        toast("warning", "Usage: /theme <dark|light>");
        return;
      }
      // Persist to ~/.coderAI/config.json so the setting survives restart.
      // The previous implementation only set process.env, which died with
      // the process — `Restart chat to apply` was a lie. We still mutate
      // process.env so a future re-resolve in the same session would pick
      // it up, but the source of truth is the config file.
      process.env.CODERAI_THEME = themeName;
      try {
        const cfgPath = path.join(os.homedir(), ".coderAI", "config.json");
        let cfg: Record<string, unknown> = {};
        try {
          const raw = fs.readFileSync(cfgPath, "utf8");
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === "object") cfg = parsed as Record<string, unknown>;
        } catch {
          // Missing or unreadable — start fresh.
        }
        cfg.theme = themeName;
        fs.mkdirSync(path.dirname(cfgPath), {recursive: true});
        fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n", "utf8");
        toast(
          "success",
          `Theme persisted as ${themeName}. Restart chat to see it applied.`,
        );
      } catch (e: any) {
        toast("warning", `Theme save failed: ${e?.message ?? e}`);
      }
      return;
    }
    case "undo":
      client.sendMessage(raw);
      return;
    default:
      toast(
        "warning",
        `Unknown command: /${head} · press / or type /help to open the menu`,
      );
  }
}

function timelineToMarkdown(items: TimelineItem[]): string {
  let md = "# CoderAI Session\n\n";
  md += `Exported: ${new Date().toISOString()}\n\n---\n\n`;
  for (const item of items) {
    switch (item.kind) {
      case "user":
        md += `**You:**\n\n${item.text}\n\n---\n\n`;
        break;
      case "assistant":
        md += `**Assistant:**\n\n${item.content}\n`;
        if (item.reasoning.trim()) {
          md += `\n<details><summary>Reasoning (${item.reasoning.length.toLocaleString()} chars)</summary>\n\n${item.reasoning.trim()}\n\n</details>\n`;
        }
        md += "\n---\n\n";
        break;
      case "tool":
        md += `**Tool:** \`${item.name}\` — ${item.ok ? "✓" : item.ok === false ? "✗" : "…"}\n\n`;
        if (item.preview) md += `> ${item.preview.replace(/\n/g, "\n> ")}\n`;
        if (item.error) md += `> ${item.error}\n`;
        md += "\n---\n\n";
        break;
      case "diff":
        md += `**Diff:** \`${item.path}\`\n\n\`\`\`diff\n${item.diff}\n\`\`\`\n\n---\n\n`;
        break;
      case "error":
        md += `**Error:** ${item.message}\n`;
        if (item.details) md += `\n\`\`\`\n${item.details}\n\`\`\`\n`;
        md += "\n---\n\n";
        break;
    }
  }
  return md;
}

function findLastAssistant(items: TimelineItem[]): string | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "assistant" && it.content.trim()) {
      return it.content.trim();
    }
  }
  return null;
}

function copyToClipboard(text: string): void {
  const base64 = Buffer.from(text, "utf8").toString("base64");
  process.stdout.write(`${ESC}]52;c;${base64}${BEL}`);
}

function summarizeTimelineItem(item: TimelineItem, max = 120): string {
  switch (item.kind) {
    case "user":
      return item.text.slice(0, max);
    case "assistant":
      return item.content.slice(0, max);
    case "tool":
      return `${item.name} ${item.preview || item.error || ""}`.trim().slice(0, max);
    case "diff":
      return `${item.path}: ${item.diff.slice(0, max)}`;
    case "error":
      return item.message.slice(0, max);
    case "toast":
      return item.message.slice(0, max);
    case "separator":
      return item.message.slice(0, max);
    case "approval":
      return item.tool.slice(0, max);
  }
}
