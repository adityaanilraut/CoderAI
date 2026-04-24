import React, {memo} from "react";
import {Box, Text} from "ink";
import type {SessionState} from "../hooks/useAgent.js";
import {theme} from "../theme.js";
import {formatTokenCount} from "../lib/format.js";
import {Dot} from "./Primitives.js";

export interface StatusBarProps {
  session: SessionState;
  narrow?: boolean;
}

/**
 * Bottom status bar.
 *
 * Redesign: no more `│` pipe separators — fields breathe with whitespace
 * and each status gets a colored dot for scannability.  A tiny 10-cell
 * context-usage meter replaces the percentage-only readout so users can
 * feel the fill rate.
 *
 *   ◆ claude-opus-4-7   anthropic     ██████░░░░ 62%  12.3k/200k   $0.048   ● safe
 */
export const StatusBar = memo(function StatusBar({
  session,
  narrow = false,
}: StatusBarProps) {
  const ctxKnown = session.ctxLimit > 0;
  const ctxPct = ctxKnown ? (session.ctxUsed / session.ctxLimit) * 100 : 0;
  const ctxColor = !ctxKnown
    ? theme.muted
    : ctxPct >= 90
      ? theme.danger
      : ctxPct >= 70
        ? theme.warning
        : theme.info;

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

  const ctxMeter = ctxKnown ? renderMeter(ctxPct, ctxColor) : null;

  const modelSeg = (
    <Box>
      <Dot color={theme.accent} glyph={theme.glyph.diamond} />
      <Text color={theme.accent} bold>
        {" "}
        {session.model || "booting…"}
      </Text>
      {session.provider ? (
        <Text color={theme.faint}>
          {theme.glyph.separator}
          {session.provider}
        </Text>
      ) : null}
    </Box>
  );

  const ctxSeg = (
    <Box>
      {ctxMeter}
      {ctxMeter ? <Text> </Text> : null}
      <Text color={ctxColor}>
        {ctxKnown ? `${ctxPct.toFixed(0)}%` : "ctx —"}
      </Text>
      {ctxKnown ? (
        <Text color={theme.faint}>
          {theme.glyph.separator}
          {formatTokenCount(session.ctxUsed)}/{formatTokenCount(session.ctxLimit)}
        </Text>
      ) : null}
    </Box>
  );

  const costSeg = (
    <Box>
      <Text color={costColor}>${session.costUsd.toFixed(4)}</Text>
      {budget > 0 ? (
        <Text color={theme.faint}>
          {theme.glyph.separator}/ ${budget.toFixed(2)}
        </Text>
      ) : null}
    </Box>
  );

  const agentsSeg =
    activeAgents > 0 ? (
      <Box>
        <Dot color={theme.info} />
        <Text color={theme.info}>
          {" "}
          {activeAgents} agent{activeAgents === 1 ? "" : "s"}
        </Text>
      </Box>
    ) : null;

  const modeSeg = (
    <Box>
      <Dot color={session.autoApprove ? theme.warning : theme.success} />
      <Text color={session.autoApprove ? theme.warning : theme.muted}>
        {" "}
        {session.autoApprove ? "YOLO" : "safe"}
      </Text>
    </Box>
  );

  const reasoningSeg = session.reasoning ? (
    <Text color={theme.faint}>
      {theme.glyph.separator}
      {session.reasoning}
    </Text>
  ) : null;

  return (
    <Box
      paddingX={theme.spacing.sm}
      marginTop={theme.spacing.sm}
      flexDirection={narrow ? "column" : "row"}
      justifyContent="space-between"
      width="100%"
    >
      {/* Left: brand · model · provider */}
      {modelSeg}

      {/* Right: ctx · cost · agents · mode */}
      <Box>
        {ctxSeg}
        <Text>{theme.glyph.separator}</Text>
        {costSeg}
        {agentsSeg ? (
          <>
            <Text>{theme.glyph.separator}</Text>
            {agentsSeg}
          </>
        ) : null}
        <Text>{theme.glyph.separator}</Text>
        {modeSeg}
        {reasoningSeg}
      </Box>
    </Box>
  );
});

/**
 * 10-cell unicode bar, filled per percent.  Uses block glyphs so it
 * matches the typographic weight of surrounding text without relying
 * on a border box.
 */
function renderMeter(pct: number, color: string) {
  const WIDTH = 10;
  const filled = Math.max(0, Math.min(WIDTH, Math.round((pct / 100) * WIDTH)));
  const empty = WIDTH - filled;
  return (
    <Box>
      <Text color={color}>{"█".repeat(filled)}</Text>
      <Text color={theme.faint}>{"░".repeat(empty)}</Text>
    </Box>
  );
}
