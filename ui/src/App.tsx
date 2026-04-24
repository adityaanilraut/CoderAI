import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Static, Text, useApp, useInput, useStdout } from "ink";
import { useAgent } from "./hooks/useAgent.js";
import { StatusBar } from "./components/StatusBar.js";
import { Prompt } from "./components/Prompt.js";
import { ToolCard } from "./components/ToolCard.js";
import { Assistant, UserBubble } from "./components/Assistant.js";
import { Diff } from "./components/Diff.js";
import { ErrorPanel } from "./components/ErrorPanel.js";
import { AgentCard } from "./components/AgentTable.js";
import { Thinking } from "./components/Thinking.js";
import { Toast } from "./components/Toast.js";
import { HelpMenu } from "./components/HelpMenu.js";
import { ApprovalPrompt } from "./components/ApprovalPrompt.js";
import { theme } from "./theme.js";
import { isTimelineItemFrozen } from "./timelineItemFrozen.js";
import type { TimelineItem } from "./hooks/useAgent.js";

export interface AppProps {
  python?: string;
  cwd?: string;
}

const CTRL_C_WINDOW_MS = 1500;
const NARROW_COLUMNS = 72;

export function App({ python, cwd }: AppProps) {
  const { session, timeline, actions, helpMenuOpen } = useAgent({ python, cwd });
  const { exit } = useApp();
  const { stdout } = useStdout();
  const columns = stdout?.columns ?? 100;
  const narrow = columns < NARROW_COLUMNS;

  const lastCtrlC = useRef(0);
  const lastColumns = useRef<number | null>(null);
  const [exitArmed, setExitArmed] = useState(false);
  const [staticTimelineEnabled, setStaticTimelineEnabled] = useState(true);
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

  useEffect(() => {
    if (timeline.length === 0) setStaticTimelineEnabled(true);
  }, [timeline.length]);

  const { lastErrorId, pendingApprovalId } = useMemo(() => {
    let errId: string | null = null;
    let apprId: string | null = null;
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
      if (errId && apprId) break;
    }
    return { lastErrorId: errId, pendingApprovalId: apprId };
  }, [timeline]);
  const approvalPending = pendingApprovalId !== null;
  const promptBusy =
    !session.connected ||
    session.thinking ||
    session.streaming ||
    helpMenuOpen ||
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
    { isActive: !helpMenuOpen },
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
    if (!staticTimelineEnabled) return;
    if (frozenCount === 0) return;

    // Ink `Static` prints completed rows once and never reflows them, so a
    // terminal resize can leave the old layout on screen. Clear once and fall
    // back to normal rendering for the rest of the session.
    stdout?.write("\u001b[2J\u001b[H");
    setStaticTimelineEnabled(false);
  }, [columns, frozenCount, staticTimelineEnabled, stdout]);

  const frozenTimeline = useMemo(
    () => (staticTimelineEnabled ? timeline.slice(0, frozenCount) : []),
    [timeline, frozenCount, staticTimelineEnabled],
  );
  // Cap the number of live items rendered.  Ink clears+redraws every live row
  // on each tick (~4-16fps).  With many live rows the ANSI cursor math causes
  // the terminal to scroll to the top.  We keep only the tail; earlier items
  // are already in <Static> or simply off-screen.
  const MAX_LIVE_ITEMS = 12;
  const liveTimeline = useMemo(() => {
    if (!staticTimelineEnabled) {
      return timeline;
    }
    const all = timeline.slice(frozenCount);
    return all.length > MAX_LIVE_ITEMS ? all.slice(-MAX_LIVE_ITEMS) : all;
  }, [timeline, frozenCount, staticTimelineEnabled]);

  const renderItem = useCallback(
    (item: TimelineItem) => {
      switch (item.kind) {
        case "user":
          return <UserBubble key={item.id} text={item.text} />;
        case "assistant":
          return (
            <Assistant
              key={item.id}
              content={item.content}
              reasoning={item.reasoning}
              streaming={item.streaming}
            />
          );
        case "tool":
          return (
            <ToolCard
              key={item.id}
              name={item.name}
              category={item.category}
              args={item.args}
              risk={item.risk}
              ok={item.ok}
              preview={item.preview}
              error={item.error}
              fullAvailable={item.fullAvailable}
            />
          );
        case "diff":
          return (
            <Diff
              key={item.id}
              path={item.path}
              diff={item.diff}
              maxLineWidth={columns - 16}
            />
          );
        case "error":
          return (
            <ErrorPanel
              key={item.id}
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
            <Toast key={item.id} level={item.level} message={item.message} />
          );
        case "approval":
          return (
            <ApprovalPrompt
              key={item.id}
              tool={item.tool}
              args={item.args}
              risk={item.risk}
              decided={item.decided}
              active={item.id === pendingApprovalId}
              onDecide={(approve) => actions.approveTool(item.id, approve)}
            />
          );
        case "agent":
          return <AgentCard key={item.id} agent={item.agent} />;
      }
    },
    [lastErrorId, pendingApprovalId, helpMenuOpen, approvalPending, columns, actions],
  );

  const empty = timeline.length === 0;

  return (
    <Box flexDirection="column">
      {empty ? <WelcomeHero session={session} /> : null}

      <Box flexDirection="column" marginTop={empty ? 0 : 1}>
        {/* Completed items — printed to stdout once and never redrawn. */}
        <Static items={frozenTimeline}>{(item) => renderItem(item)}</Static>

        {/* Active items — re-rendered freely as they update. */}
        {liveTimeline.map((item) => renderItem(item))}

        <Thinking active={session.thinking} />

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
      </Box>

      <Box marginTop={1}>
        <Prompt
          onSubmit={actions.send}
          disabled={promptBusy}
          placeholder={
            helpMenuOpen
              ? "Esc closes command menu"
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
 * First-paint hero shown until the user sends their first message.
 * A branded diamond, the model being used, and a three-tip cheat sheet
 * so the blank transcript doesn't feel cold.
 */
function WelcomeHero({ session }: { session: ReturnType<typeof useAgent>["session"] }) {
  return (
    <Box flexDirection="column" paddingX={2} marginTop={1} marginBottom={1}>
      <Box>
        <Text color={theme.accent} bold>
          {theme.glyph.diamond}  CoderAI
        </Text>
        <Text color={theme.faint}>
          {"   "}
          AI Powered Coding Agent
        </Text>
      </Box>

      {session.model ? (
        <Box marginTop={1}>
          <Text color={theme.faint}>connected to </Text>
          <Text color={theme.accent}>{session.model}</Text>
          {session.provider ? (
            <Text color={theme.faint}>
              {"  "}
              via {session.provider}
            </Text>
          ) : null}
        </Box>
      ) : (
        <Box marginTop={1}>
          <Text color={theme.faint}>booting agent…</Text>
        </Box>
      )}

      <Box marginTop={1} flexDirection="column">
        <Text color={theme.muted}>
          <Text color={theme.role.user}>{theme.glyph.arrowRun}</Text> ask for a
          feature, a refactor, or a bug fix
        </Text>
        <Text color={theme.muted}>
          <Text color={theme.role.user}>{theme.glyph.arrowRun}</Text> type{" "}
          <Text color={theme.accent}>/help</Text> for commands
        </Text>
        <Text color={theme.muted}>
          <Text color={theme.role.user}>{theme.glyph.arrowRun}</Text> press{" "}
          <Text color={theme.accent}>esc</Text> to interrupt, ctrl+c twice to
          quit
        </Text>
      </Box>
    </Box>
  );
}
