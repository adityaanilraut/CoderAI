import React from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import type {AgentInfo, AgentStatus} from "../protocol.js";

const FINISHED: AgentStatus[] = ["done", "error", "cancelled"];

/** Single-agent card rendered inline in the timeline for main and sub-agents. */
export function AgentCard({agent}: {agent: AgentInfo}) {
  const finished = FINISHED.includes(agent.status);
  return (
    <Box
      borderStyle="round"
      borderColor={finished ? theme.borderSoft : theme.info}
      paddingX={1}
      marginBottom={1}
      flexDirection="column"
    >
      <Box justifyContent="space-between">
        <Box>
          <StatusBadge status={agent.status} />
          <Text> </Text>
          <Text bold color={finished ? theme.muted : theme.text}>
            {agent.name}
          </Text>
          {agent.role ? (
            <Text color={theme.muted}> ({agent.role})</Text>
          ) : null}
        </Box>
        <Text color={theme.muted}>
          {(agent.elapsedMs / 1000).toFixed(1)}s
        </Text>
      </Box>
      <Box>
        <Text color={theme.muted}>
          {" "}
          {formatTokens(agent.tokens)} tok · ${agent.costUsd.toFixed(4)}
          {!finished && agent.tool ? (
            <Text color={theme.warning}> · {agent.tool}</Text>
          ) : null}
        </Text>
      </Box>
      {agent.task ? (
        <Box
          marginTop={1}
          borderStyle="single"
          borderColor={theme.borderSoft}
          paddingX={1}
        >
          <Text color={theme.muted} italic>
            {truncate(agent.task, 160)}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function StatusBadge({status}: {status: AgentStatus}) {
  switch (status) {
    case "thinking":
      return (
        <Text color={theme.accent}>
          <Spinner type="dots" />
        </Text>
      );
    case "tool_call":
      return <Text color={theme.warning}>⚙</Text>;
    case "waiting_for_user":
      return <Text color={theme.info}>⏸</Text>;
    case "done":
      return <Text color={theme.success}>✓</Text>;
    case "error":
      return <Text color={theme.danger}>✗</Text>;
    case "cancelled":
      return <Text color={theme.muted}>⊘</Text>;
    default:
      return <Text color={theme.muted}>·</Text>;
  }
}

function formatTokens(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}
