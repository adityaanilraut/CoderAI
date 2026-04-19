import React, {useEffect, useMemo, useRef, useState} from "react";
import {Box, Text, useApp, useInput, useStdout} from "ink";
import Spinner from "ink-spinner";
import os from "node:os";
import {useAgent} from "./hooks/useAgent.js";
import {StatusBar} from "./components/StatusBar.js";
import {Prompt} from "./components/Prompt.js";
import {ToolCard} from "./components/ToolCard.js";
import {Assistant, UserBubble} from "./components/Assistant.js";
import {Diff} from "./components/Diff.js";
import {ErrorPanel} from "./components/ErrorPanel.js";
import {AgentTable} from "./components/AgentTable.js";
import {Thinking} from "./components/Thinking.js";
import {Toast} from "./components/Toast.js";
import {ApprovalPrompt} from "./components/ApprovalPrompt.js";
import {theme} from "./theme.js";

export interface AppProps {
  python?: string;
  cwd?: string;
}

const CTRL_C_WINDOW_MS = 1500;
const NARROW_COLUMNS = 72;

export function App({python, cwd}: AppProps) {
  const {session, timeline, actions} = useAgent({python, cwd});
  const {exit} = useApp();
  const columns = useTerminalColumns();
  const narrow = columns < NARROW_COLUMNS;

  const lastCtrlC = useRef(0);
  const [exitArmed, setExitArmed] = useState(false);
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

  const promptBusy =
    !session.connected || session.thinking || session.streaming;

  useInput((input, key) => {
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
  });

  const agentList = useMemo(
    () => Object.values(session.agents),
    [session.agents],
  );

  // Only the most recent items of a given kind listen for keystrokes so
  // every error panel / approval dialog doesn't fight over the same keys.
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

  return (
    <Box flexDirection="column">
      {session.connected ? (
        <Header session={session} narrow={narrow} />
      ) : (
        <ConnectingSplash />
      )}

      <Box flexDirection="column" marginTop={1}>
        {timeline.map((item) => {
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
                  canExpand={item.id === lastErrorId && promptBusy}
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
          }
        })}

        {session.thinking ? <Thinking active /> : null}

        {agentList.length > 0 ? <AgentTable agents={agentList} /> : null}
      </Box>

      <Box marginTop={1}>
        <Prompt
          onSubmit={actions.send}
          disabled={promptBusy}
          placeholder={
            !session.connected
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
      borderStyle="bold"
      borderColor={theme.accent}
      paddingX={1}
      flexDirection="column"
    >
      <Box
        flexDirection={narrow ? "column" : "row"}
        justifyContent="space-between"
      >
        <Box>
          <Text color={theme.accent} bold>
            CoderAI
          </Text>
          <Text color={theme.muted}> v{session.version || "—"}</Text>
        </Box>
        <Box>
          <Text color={theme.muted}>
            {session.cwd ? shortenCwd(session.cwd) : ""}
          </Text>
        </Box>
      </Box>
      {session.projectSummary ? (
        <Box>
          <Text color={theme.muted} italic>
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
      borderStyle="bold"
      borderColor={theme.accentDim}
      paddingX={1}
      flexDirection="column"
    >
      <Box>
        <Text color={theme.accent} bold>
          <Spinner type="dots" />
          <Text> CoderAI</Text>
        </Text>
        <Text color={theme.muted}> · starting agent…</Text>
      </Box>
      <Box>
        <Text color={theme.muted} italic>
          Spawning Python subprocess and loading providers. This usually
          takes a second or two.
        </Text>
      </Box>
    </Box>
  );
}

function useTerminalColumns(): number {
  const {stdout} = useStdout();
  const [cols, setCols] = useState<number>(stdout?.columns ?? 100);

  useEffect(() => {
    if (!stdout) return;
    const onResize = () => setCols(stdout.columns ?? 100);
    stdout.on("resize", onResize);
    return () => {
      stdout.off("resize", onResize);
    };
  }, [stdout]);

  return cols;
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
