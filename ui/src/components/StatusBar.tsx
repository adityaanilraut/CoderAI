import React from "react";
import {Box, Text} from "ink";
import type {SessionState} from "../hooks/useAgent.js";
import {theme} from "../theme.js";

export function StatusBar({
  session,
  narrow = false,
}: {
  session: SessionState;
  narrow?: boolean;
}) {
  const ctxKnown = session.ctxLimit > 0;
  const ctxPct = ctxKnown ? (session.ctxUsed / session.ctxLimit) * 100 : 0;
  const ctxColor = !ctxKnown
    ? theme.muted
    : ctxPct >= 90
      ? theme.danger
      : ctxPct >= 70
        ? theme.warning
        : theme.muted;

  const budget = session.budgetUsd || 0;
  const costPct = budget > 0 ? (session.costUsd / budget) * 100 : 0;
  const costColor =
    budget <= 0
      ? theme.muted
      : costPct >= 80
        ? theme.danger
        : costPct >= 50
          ? theme.warning
          : theme.success;

  const activeAgents = Object.values(session.agents).filter(
    (a) => !["done", "error", "cancelled"].includes(a.status),
  ).length;

  // Segments so we can either join them with pipes (wide) or stack them
  // in a column (narrow terminals) without duplicating JSX.
  const left = (
    <Box>
      <Text color={theme.accent} bold>
        {session.model || "…"}
      </Text>
      <Text color={theme.muted}> · {session.provider || "not connected"}</Text>
    </Box>
  );

  const ctxSeg = (
    <Text color={ctxColor}>
      {ctxKnown ? (
        <>
          ctx {ctxPct.toFixed(0)}%{" "}
          <Text color={theme.muted}>
            ({formatTokens(session.ctxUsed)}/{formatTokens(session.ctxLimit)})
          </Text>
        </>
      ) : (
        <>ctx —</>
      )}
    </Text>
  );

  const costSeg = (
    <Text color={costColor}>
      ${session.costUsd.toFixed(4)}
      {budget > 0 ? (
        <Text color={theme.muted}> / ${budget.toFixed(2)}</Text>
      ) : null}
    </Text>
  );

  const agentsSeg = (
    <Text color={activeAgents ? theme.info : theme.muted}>
      {activeAgents} agent{activeAgents === 1 ? "" : "s"}
    </Text>
  );

  const modeSeg = (
    <Text color={session.autoApprove ? theme.warning : theme.muted}>
      {session.autoApprove ? "YOLO" : "safe"}
    </Text>
  );

  if (narrow) {
    return (
      <Box
        borderStyle="round"
        borderColor={theme.accentDim}
        paddingX={1}
        flexDirection="column"
      >
        {left}
        <Box>
          {ctxSeg}
          <Text color={theme.muted}>  ·  </Text>
          {costSeg}
        </Box>
        <Box>
          {agentsSeg}
          <Text color={theme.muted}>  ·  </Text>
          {modeSeg}
        </Box>
      </Box>
    );
  }

  return (
    <Box
      borderStyle="round"
      borderColor={theme.accentDim}
      paddingX={1}
      justifyContent="space-between"
    >
      {left}
      <Box>
        {ctxSeg}
        <Text color={theme.muted}>  │  </Text>
        {costSeg}
        <Text color={theme.muted}>  │  </Text>
        {agentsSeg}
        <Text color={theme.muted}>  │  </Text>
        {modeSeg}
      </Box>
    </Box>
  );
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}
