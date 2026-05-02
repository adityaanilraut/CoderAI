import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Static, Text, useApp, useInput, useStdout } from "ink";
import { useAgent } from "./hooks/useAgent.js";
import { StatusBar } from "./components/StatusBar.js";
import { Prompt } from "./components/Prompt.js";
import { ToolCard } from "./components/ToolCard.js";
import { Assistant, UserBubble } from "./components/Assistant.js";
import { Diff } from "./components/Diff.js";
import { ErrorPanel } from "./components/ErrorPanel.js";
import { AgentTree } from "./components/AgentTable.js";
import { Thinking } from "./components/Thinking.js";
import { Toast } from "./components/Toast.js";
import { ProgressBar } from "./components/ProgressBar.js";
import { HelpMenu } from "./components/HelpMenu.js";
import { ModelMenu } from "./components/ModelMenu.js";
import { ReasoningMenu } from "./components/ReasoningMenu.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import { theme } from "./theme.js";
import { isTimelineItemFrozen } from "./lib/timelineItemFrozen.js";
import { SearchOverlay } from "./components/SearchOverlay.js";
import type { TimelineItem } from "./hooks/useAgent.js";

export interface AppProps {
  python?: string;
  cwd?: string;
}

const CTRL_C_WINDOW_MS = 1500;
// Cap of timeline items kept in the live (re-rendered) region. Anything
// older is moved into the Static (write-once) prefix to keep Ink's per-tick
// redraw cheap. See `staticTimelineEpoch` for how resize handling stays
// efficient over a long session.
const MAX_LIVE_ITEMS = 12;

export function App({ python, cwd }: AppProps) {
  const { session, timeline, actions, helpMenuOpen, modelMenuOpen, reasoningMenuOpen, searchOpen, searchFilter } = useAgent({ python, cwd });
  const { exit } = useApp();
  const { stdout } = useStdout();
  const columns = stdout?.columns ?? 100;
  const narrow = columns < theme.layout.narrowCols;

  const lastCtrlC = useRef(0);
  const lastColumns = useRef<number | null>(null);
  const [exitArmed, setExitArmed] = useState(false);
  // Bumping the epoch invalidates the existing Static block (Ink keys it on
  // a hidden internal counter — but our slice math depends on this epoch to
  // re-establish a fresh frozen prefix). Bumped on terminal resize so the
  // post-resize layout is computed from current widths, without falling
  // back to "everything is live" forever.
  const [staticTimelineEpoch, setStaticTimelineEpoch] = useState(0);
  const armTimer = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (!exitArmed) return;
    armTimer.current = setTimeout(
      () => setExitArmed(false),
      CTRL_C_WINDOW_MS,
    );
    return () => {
      if (armTimer.current) clearTimeout(armTimer.current);
    };
  }, [exitArmed]);

  const { lastErrorId, pendingApprovalId, lastAssistantId } = useMemo(() => {
    let errId: string | null = null;
    let apprId: string | null = null;
    let asstId: string | null = null;
    for (let i = timeline.length - 1; i >= 0; i--) {
      const it = timeline[i];
      if (!errId && it.kind === "error") errId = it.id;
      if (
        !apprId &&
        it.kind === "approval" &&
        it.decided === "pending"
      ) {
        apprId = it.id;
      }
      if (!asstId && it.kind === "assistant") asstId = it.id;
      if (errId && apprId && asstId) break;
    }
    return { lastErrorId: errId, pendingApprovalId: apprId, lastAssistantId: asstId };
  }, [timeline]);
  const approvalPending = pendingApprovalId !== null;
  const promptBusy =
    !session.connected ||
    session.thinking ||
    session.streaming ||
    helpMenuOpen ||
    modelMenuOpen ||
    reasoningMenuOpen ||
    searchOpen ||
    approvalPending;

  // Ref so renderItem can read the latest value without being in its dep array.
  // promptBusy changes on every streaming tick (session.streaming), so keeping
  // it out of useCallback deps prevents recreating renderItem 16+ times/second.
  const promptBusyRef = useRef(promptBusy);
  promptBusyRef.current = promptBusy;

  useInput(
    (input, key) => {
      if (key.escape && (session.thinking || session.streaming)) {
        actions.cancel();
        return;
      }

      // Ctrl+R is now owned by the Prompt (reverse-i-search). Reveal
      // reasoning still has the /think slash command and the inline
      // "▸ reasoning" hint surfaced under the latest assistant turn —
      // no keystroke is needed.

      if (key.ctrl && input === "c") {
        const now = Date.now();
        const withinWindow = now - lastCtrlC.current < CTRL_C_WINDOW_MS;
        if (withinWindow) {
          actions.exit();
          setTimeout(() => exit(), 200);
          return;
        }
        lastCtrlC.current = now;
        setExitArmed(true);
        if (session.thinking || session.streaming) actions.cancel();
      }
    },
    { isActive: !helpMenuOpen && !modelMenuOpen && !reasoningMenuOpen && !searchOpen },
  );

  // Split timeline into a frozen prefix (handed to Static — printed once, never
  // redrawn) and a live suffix (re-rendered normally for active updates).
  //
  // IMPORTANT: We aggressively freeze items to keep the live region as small as
  // possible.  Ink redraws the entire live region on every state change (timers,
  // stream ticks, status updates).  If the live region grows large the ANSI
  // cursor-repositioning Ink performs causes the terminal viewport to jump to
  // the top — the "scroll to top on refresh" bug.
  const frozenCount = useMemo(() => {
    let i = 0;
    while (i < timeline.length && isTimelineItemFrozen(timeline[i])) i++;
    return i;
  }, [timeline]);

  useEffect(() => {
    if (lastColumns.current === null) {
      lastColumns.current = columns;
      return;
    }
    if (lastColumns.current === columns) return;
    lastColumns.current = columns;
    if (frozenCount === 0) return;

    // Ink `Static` prints completed rows once and never reflows them, so a
    // terminal resize can leave the old layout on screen. Bump the epoch to
    // re-bake a fresh Static block from current widths.
    //
    // ESC[J clears from cursor to end of screen. We used to send ESC[2J
    // ESC[H (full clear + home cursor), which also wiped scrollback — users
    // often want prior session output preserved (long tracebacks, commands
    // they ran), so the gentler form keeps history above the cursor intact.
    stdout?.write("\u001b[J");
    setStaticTimelineEpoch((e) => e + 1);
  }, [columns, frozenCount, stdout]);

  const frozenTimeline = useMemo(
    () => timeline.slice(0, frozenCount),
    [timeline, frozenCount],
  );
  // Cap the live region. Ink clears+redraws every live row on each tick
  // (~4-16fps); a large live region causes the ANSI cursor math to scroll
  // to the top.
  //
  // Subtlety: a naive slice(-MAX_LIVE_ITEMS) drops the oldest live items —
  // which can include *still-running* tool calls during a parallel burst
  // (e.g. an assistant spawning 15 read_file calls at once). Those tools
  // stop receiving their phase-update render until the burst dies down.
  //
  // So we cap at MAX_LIVE_ITEMS *or* the count needed to retain every
  // non-frozen item, whichever is larger. This way running work is never
  // hidden; only frozen-but-not-yet-Static-promoted items are evicted.
  const liveTimeline = useMemo(() => {
    const all = timeline.slice(frozenCount);
    if (all.length <= MAX_LIVE_ITEMS) return all;

    let nonFrozenTotal = 0;
    for (const it of all) if (!isTimelineItemFrozen(it)) nonFrozenTotal++;

    let kept = 0;
    let nonFrozenSeen = 0;
    for (let i = all.length - 1; i >= 0; i--) {
      kept++;
      if (!isTimelineItemFrozen(all[i])) nonFrozenSeen++;
      if (kept >= MAX_LIVE_ITEMS && nonFrozenSeen >= nonFrozenTotal) {
        return all.slice(i);
      }
    }
    return all;
  }, [timeline, frozenCount]);

  const renderItem = useCallback(
    (item: TimelineItem) => {
      switch (item.kind) {
        case "user":
          return <UserBubble key={`user-${item.id}`} text={item.text} />;
        case "assistant":
          return (
            <Assistant
              key={`assistant-${item.id}`}
              content={item.content}
              reasoning={item.reasoning}
              streaming={item.streaming}
              showReasoning={session.verbose}
              isLatest={item.id === lastAssistantId && !promptBusyRef.current}
              cwd={session.cwd}
            />
          );
        case "tool":
          return (
            <ToolCard
              key={`tool-${item.id}`}
              name={item.name}
              category={item.category}
              args={item.args}
              risk={item.risk}
              ok={item.ok}
              preview={item.preview}
              error={item.error}
              fullAvailable={item.fullAvailable}
              verbose={session.verbose}
            />
          );
        case "diff":
          return (
            <Diff
              key={`diff-${item.id}`}
              path={item.path}
              diff={item.diff}
              maxLineWidth={columns - 16}
              verbose={session.verbose}
            />
          );
        case "error":
          return (
            <ErrorPanel
              key={`error-${item.id}`}
              category={item.category}
              message={item.message}
              hint={item.hint}
              details={item.details}
              canExpand={
                item.id === lastErrorId &&
                !helpMenuOpen &&
                !approvalPending
              }
              promptActive={!promptBusyRef.current}
            />
          );
        case "toast":
          return (
            <Toast key={`toast-${item.id}`} level={item.level} message={item.message} />
          );
        case "approval":
          return (
            <ApprovalPrompt
              key={`approval-${item.id}`}
              tool={item.tool}
              args={item.args}
              risk={item.risk}
              decided={item.decided}
              active={item.id === pendingApprovalId}
              onDecide={(approve, always) => actions.approveTool(item.id, approve, always)}
            />
          );
      }
    },
    [lastErrorId, pendingApprovalId, lastAssistantId, helpMenuOpen, approvalPending, columns, actions, session.verbose, session.cwd],
  );

  const empty = timeline.length === 0;

  return (
    <Box flexDirection="column">
      {empty ? <WelcomeHero session={session} narrow={narrow} /> : null}

      <Box flexDirection="column" marginTop={empty ? 0 : 1}>
        {/* Completed items — printed to stdout once and never redrawn.
            `key={staticTimelineEpoch}` re-mounts the block after a terminal
            resize so the new layout is computed with current widths. */}
        <Static key={staticTimelineEpoch} items={frozenTimeline}>
          {(item) => renderItem(item)}
        </Static>

        {/* Active items — re-rendered freely as they update. */}
        {liveTimeline.map((item) => renderItem(item))}

        <Thinking active={session.thinking} detail={thinkingDetail(session.agents)} />

        {session.progress ? (
          <ProgressBar
            label={session.progress.label}
            current={session.progress.current}
            total={session.progress.total}
            kind={session.progress.kind}
          />
        ) : null}

        {helpMenuOpen ? (
          <HelpMenu
            maxWidth={columns}
            onClose={actions.closeHelpMenu}
            onPick={(slash) => {
              actions.closeHelpMenu();
              actions.send(slash);
            }}
          />
        ) : null}

        {modelMenuOpen ? (
          <ModelMenu
            models={session.availableModels}
            current={session.model}
            maxWidth={columns}
            onClose={actions.closeModelMenu}
            onPick={(model) => {
              actions.closeModelMenu();
              actions.send(`/model ${model}`);
            }}
          />
        ) : null}

        {reasoningMenuOpen ? (
          <ReasoningMenu
            current={session.reasoning}
            maxWidth={columns}
            onClose={actions.closeReasoningMenu}
            onPick={(effort) => {
              actions.closeReasoningMenu();
              actions.send(`/reasoning ${effort}`);
            }}
          />
        ) : null}

        {searchOpen ? (
          <SearchOverlay
            timeline={timeline}
            filter={searchFilter}
            onFilterChange={actions.setSearchFilter}
            onClose={actions.closeSearch}
            maxWidth={columns}
          />
        ) : null}
      </Box>

      <AgentTree
        agents={session.agents}
        finishedAt={session.agentsFinishedAt}
        width={columns}
      />

      <Box marginTop={1}>
        <Prompt
          onSubmit={actions.send}
          disabled={promptBusy}
          cwd={session.cwd}
          placeholder={
            helpMenuOpen
              ? "Esc closes command menu"
              : modelMenuOpen
                ? "Esc closes model picker"
                : reasoningMenuOpen
                  ? "Esc closes reasoning picker"
                  : !session.connected
                ? "starting agent…"
                : session.thinking
                  ? "thinking…"
                  : session.streaming
                    ? "streaming…"
                    : undefined
          }
          exitHint={exitArmed}
        />
      </Box>

      <StatusBar session={session} narrow={narrow} />
    </Box>
  );
}

/**
 * First-paint greeting shown until the user sends their first message.
 * Wide terminals get one row (`coderai · model · cwd`); narrow terminals
 * stack the cwd line so a long path doesn't overflow.
 */
function WelcomeHero({
  session,
  narrow,
}: {
  session: ReturnType<typeof useAgent>["session"];
  narrow: boolean;
}) {
  const cwd = session.cwd
    ? session.cwd.replace(process.env.HOME ?? "", "~")
    : "";
  const sep = (
    <>
      {"  "}
      <Text color={theme.faint}>·</Text>
      {"  "}
    </>
  );
  return (
    <Box flexDirection="column" paddingX={2} marginTop={1} marginBottom={1}>
      <Text color={theme.muted}>
        coderai
        {session.model ? (
          <>
            {sep}
            {session.model}
          </>
        ) : (
          <Text color={theme.faint}>  · booting…</Text>
        )}
        {cwd && !narrow ? (
          <>
            {sep}
            {cwd}
          </>
        ) : null}
      </Text>
      {cwd && narrow ? (
        <Text color={theme.muted}>{cwd}</Text>
      ) : null}
      <Box flexDirection="column" marginTop={1}>
        <Text color={theme.textSoft}>Try:</Text>
        {SUGGESTED_PROMPTS.map((p, i) => (
          <Text key={i} color={theme.muted}>
            {"  "}
            <Text color={theme.faint}>{theme.glyph.arrowRun}</Text> {p}
          </Text>
        ))}
        <Box marginTop={1}>
          <Text color={theme.muted}>
            Type <Text color={theme.accent} bold>/</Text> to browse commands{" "}
            <Text color={theme.faint}>{theme.glyph.dot}</Text>{" "}
            <Text color={theme.accent} bold>/help</Text> for shortcuts
          </Text>
        </Box>
      </Box>
    </Box>
  );
}

/** Suggested first prompts shown on a fresh session. Kept short so a 72-col
 *  terminal fits each on one line. */
const SUGGESTED_PROMPTS = [
  "explain what this codebase does",
  "find and fix bugs in the recently changed files",
  "add a test for <file>",
];

/**
 * Pick a short label describing what the agent is currently doing, for the
 * thinking spinner. Prefers a sub-agent's running tool, then its task, then
 * the main agent's tool/task. Returns undefined when nothing useful is
 * known so the spinner falls back to the bare "thinking" text.
 */
function thinkingDetail(
  agents: ReturnType<typeof useAgent>["session"]["agents"],
): string | undefined {
  const live = Object.values(agents).filter(
    (a) => !["done", "error", "cancelled"].includes(a.status),
  );
  if (live.length === 0) return undefined;
  // A sub-agent (parentId set) is more interesting than the root agent
  // because the parent is usually just orchestrating.
  const subagent = live.find((a) => a.parentId);
  const focus = subagent ?? live[0];
  return focus.tool || focus.task || focus.name || undefined;
}
