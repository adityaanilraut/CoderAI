import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {Box, Static, Text, useApp, useInput, useStdout} from "ink";
import Spinner from "ink-spinner";
import os from "node:os";
import {useAgent} from "./hooks/useAgent.js";
import {StatusBar} from "./components/StatusBar.js";
import {Prompt} from "./components/Prompt.js";
import {ToolCard} from "./components/ToolCard.js";
import {Assistant, UserBubble} from "./components/Assistant.js";
import {Diff} from "./components/Diff.js";
import {ErrorPanel} from "./components/ErrorPanel.js";
import {AgentCard} from "./components/AgentTable.js";
import {Thinking} from "./components/Thinking.js";
import {Toast} from "./components/Toast.js";
import {HelpMenu} from "./components/HelpMenu.js";
import {ApprovalPrompt} from "./components/ApprovalPrompt.js";
import {theme} from "./theme.js";
import {isTimelineItemFrozen} from "./timelineItemFrozen.js";
import type {TimelineItem} from "./hooks/useAgent.js";

export interface AppProps {
  python?: string;
  cwd?: string;
}

const CTRL_C_WINDOW_MS = 1500;
const NARROW_COLUMNS = 72;

export function App({python, cwd}: AppProps) {
  const {session, timeline, actions, helpMenuOpen} = useAgent({python, cwd});
  const {exit} = useApp();
  const {stdout} = useStdout();
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

  const {lastErrorId, pendingApprovalId} = useMemo(() => {
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
    return {lastErrorId: errId, pendingApprovalId: apprId};
  }, [timeline]);
  const approvalPending = pendingApprovalId !== null;
  const promptBusy =
    !session.connected ||
    session.thinking ||
    session.streaming ||
    helpMenuOpen ||
    approvalPending;

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
    {isActive: !helpMenuOpen},
  );

  // Split timeline into a frozen prefix (handed to Static — printed once, never
  // redrawn) and a live suffix (re-rendered normally for active updates).
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
  const liveTimeline = useMemo(
    () =>
      staticTimelineEnabled ? timeline.slice(frozenCount) : timeline,
    [timeline, frozenCount, staticTimelineEnabled],
  );

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
              promptActive={!promptBusy}
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
    [lastErrorId, pendingApprovalId, promptBusy, columns, actions],
  );

  return (
    <Box flexDirection="column">
      {session.connected ? (
        <Header session={session} narrow={narrow} />
      ) : (
        <ConnectingSplash />
      )}

      <Box flexDirection="column" marginTop={1}>
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

function Header({
  session,
  narrow,
}: {
  session: ReturnType<typeof useAgent>["session"];
  narrow: boolean;
}) {
  return (
    <Box
      borderStyle="double"
      borderColor={theme.accent}
      paddingX={1}
      flexDirection="column"
    >
      <Box
        flexDirection={narrow ? "column" : "row"}
        justifyContent="space-between"
      >
        <Box flexDirection={narrow ? "column" : "row"}>
          <Text color={theme.accent} bold>
            CoderAI
          </Text>
          <Text color={theme.muted}>
            {" "}
            v{session.version || "—"} · autonomous terminal coding assistant
          </Text>
        </Box>
        <Box flexDirection={narrow ? "column" : "row"}>
          <Text color={theme.info}>
            {session.cwd ? shortenCwd(session.cwd) : ""}
          </Text>
          {session.model ? (
            <Text color={theme.muted}>
              {narrow ? "" : "  "}
              runtime {session.provider || "provider"} / {session.model}
            </Text>
          ) : null}
        </Box>
      </Box>
      <Box
        marginTop={1}
        flexDirection={narrow ? "column" : "row"}
      >
        <HeaderBadge
          label={session.autoApprove ? "YOLO MODE" : "SAFE MODE"}
          color={session.autoApprove ? theme.warning : theme.success}
        />
        <Text color={theme.muted}>{narrow ? "" : " "}</Text>
        <HeaderBadge
          label={`REASONING ${session.reasoning.toUpperCase()}`}
          color={theme.info}
        />
        <Text color={theme.muted}>{narrow ? "" : " "}</Text>
        <HeaderBadge
          label={`${Object.keys(session.agents).length} AGENTS TRACKED`}
          color={theme.accentSoft}
        />
      </Box>
      {session.projectSummary ? (
        <Box marginTop={1}>
          <Text color={theme.text} italic>
            {truncate(session.projectSummary.trim(), 120)}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

function ConnectingSplash() {
  return (
    <Box
      borderStyle="double"
      borderColor={theme.accentDim}
      paddingX={1}
      flexDirection="column"
    >
      <Box>
        <Text color={theme.accent} bold>
          <Spinner type="dots" />
        </Text>
        <Text color={theme.accent} bold> CoderAI</Text>
        <Text color={theme.muted}> · booting terminal workspace…</Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        <Text color={theme.info}>multi-agent orchestration · tool registry · session state</Text>
        <Text color={theme.muted} italic>
          Spawning Python subprocess and loading providers. This usually
          takes a second or two.
        </Text>
      </Box>
    </Box>
  );
}

function HeaderBadge({
  label,
  color,
}: {
  label: string;
  color: string;
}) {
  return (
    <Text backgroundColor={color} color="black" bold>
      {" " + label + " "}
    </Text>
  );
}


function shortenCwd(cwd: string): string {
  // `os.homedir()` handles Windows (USERPROFILE) and Unix (HOME) uniformly,
  // unlike reading `process.env.HOME` directly.
  const home = safeHomedir();
  if (home && cwd.startsWith(home)) return "~" + cwd.slice(home.length);
  return cwd;
}

function safeHomedir(): string | null {
  try {
    return os.homedir() || null;
  } catch {
    return null;
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}
