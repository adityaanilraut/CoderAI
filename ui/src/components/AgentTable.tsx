import React, {useMemo} from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import type {AgentInfo, AgentStatus} from "../protocol.js";

const FINISHED: AgentStatus[] = ["done", "error", "cancelled"];

export function AgentTable({agents}: {agents: AgentInfo[]}) {
  const {active, finished} = useMemo(() => {
    const a: AgentInfo[] = [];
    const f: AgentInfo[] = [];
    for (const agent of agents) {
      if (FINISHED.includes(agent.status)) f.push(agent);
      else a.push(agent);
    }
    return {active: a, finished: f};
  }, [agents]);

  if (active.length === 0 && finished.length === 0) return null;

  // Hide the whole block once every agent has finished. Users still see
  // individual tool cards; a permanent table of ✓s adds no value.
  if (active.length === 0) {
    return (
      <Box marginBottom={1}>
        <Text color={theme.muted}>
          ✓ {finished.length} sub-agent{finished.length === 1 ? "" : "s"}{" "}
          finished
        </Text>
      </Box>
    );
  }

  const roots = active.filter((a) => !a.parentId);
  const childrenOf = (id: string) =>
    active.filter((a) => a.parentId === id);

  return (
    <Box
      borderStyle="round"
      borderColor={theme.accentDim}
      flexDirection="column"
      paddingX={1}
      marginBottom={1}
    >
      <Box justifyContent="space-between">
        <Text bold color={theme.accent}>
          Agents ({active.length} active)
        </Text>
        {finished.length > 0 ? (
          <Text color={theme.muted}>+ {finished.length} finished</Text>
        ) : null}
      </Box>
      {roots.map((r, idx) => (
        <AgentNode
          key={r.id}
          agent={r}
          allAgents={active}
          depth={0}
          isLast={idx === roots.length - 1}
        />
      ))}
    </Box>
  );
}

function AgentNode({
  agent,
  allAgents,
  depth,
  isLast,
}: {
  agent: AgentInfo;
  allAgents: AgentInfo[];
  depth: number;
  isLast: boolean;
}) {
  const childrenList = allAgents.filter((a) => a.parentId === agent.id);
  const indent = "  ".repeat(depth);
  const prefix = depth === 0 ? "◆" : isLast ? "└─" : "├─";
  return (
    <Box flexDirection="column">
      <Box>
        <Text color={theme.muted}>{indent}</Text>
        <Text color={theme.muted}>{prefix} </Text>
        <StatusBadge status={agent.status} />
        <Text> </Text>
        <Text bold>{agent.name}</Text>
        {agent.role ? (
          <Text color={theme.muted}> ({agent.role})</Text>
        ) : null}
        <Text color={theme.muted}>
          {"  "}
          {formatTokens(agent.tokens)} tok · ${agent.costUsd.toFixed(4)} ·{" "}
          {(agent.elapsedMs / 1000).toFixed(1)}s
          {agent.tool ? (
            <Text color={theme.warning}> · {agent.tool}</Text>
          ) : null}
        </Text>
      </Box>
      {childrenList.map((c, i) => (
        <AgentNode
          key={c.id}
          agent={c}
          allAgents={allAgents}
          depth={depth + 1}
          isLast={i === childrenList.length - 1}
        />
      ))}
    </Box>
  );
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
