import React, {memo, useEffect, useState} from "react";
import {Box, Text, useStdout} from "ink";
import type {SessionState} from "../hooks/useAgent.js";
import {theme} from "../theme.js";
import {formatTokenCount, formatCost, truncateSmart} from "../lib/format.js";
import {Dot} from "./Primitives.js";

export interface StatusBarProps {
  session: SessionState;
  narrow?: boolean;
}

/** Width breakpoint between "compact row" and "everything in one row". Below
 *  this we drop low-priority fields (reasoning, duration) so the high-signal
 *  ones (ctx, cost, mode) don't wrap mid-segment. */
const COMPACT_COLS = 100;

/**
 * Bottom status bar.
 *
 * Redesign: no more `│` pipe separators — fields breathe with whitespace
 * and each status gets a colored dot for scannability.  A tiny 10-cell
 * context-usage meter pairs with the absolute fraction (no redundant %).
 *
 *   ◆ claude-opus-4-7   anthropic     ██████░░░░ 12.3k/200k   $0.048   ● safe
 */
export const StatusBar = memo(function StatusBar({
  session,
  narrow = false,
}: StatusBarProps) {
  const {stdout} = useStdout();
  const columns = stdout?.columns ?? 100;
  // Three tiers: narrow stacks vertically, compact drops reasoning+duration
  // and truncates model/provider, wide shows everything. Without the compact
  // tier a long model id ("claude-opus-4-7") + provider + ctx + cost + mode +
  // reasoning + 1:23 elapsed easily overflowed 100 cols and wrapped.
  const compact = !narrow && columns < COMPACT_COLS;
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

  const disconnected = session.sessionStartedAt !== null && !session.connected;

  const modelLabel = disconnected
    ? "disconnected"
    : truncateSmart(session.model || "booting…", compact ? 22 : 64);
  const modelSeg = (
    <Box>
      <Dot
        color={disconnected ? theme.danger : theme.accent}
        glyph={disconnected ? theme.glyph.cross : theme.glyph.diamond}
      />
      <Text color={disconnected ? theme.danger : theme.accent} bold>
        {" "}
        {modelLabel}
      </Text>
      {disconnected ? (
        <Text color={theme.faint}>
          {theme.glyph.separator}
          restart coderAI chat
        </Text>
      ) : session.provider && !compact ? (
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
      {ctxKnown ? (
        <Text color={ctxColor}>
          {formatTokenCount(session.ctxUsed)}/{formatTokenCount(session.ctxLimit)}
        </Text>
      ) : (
        <Text color={ctxColor}>ctx —</Text>
      )}
    </Box>
  );

  const costSeg = (
    <Box>
      <Text color={costColor}>{formatCost(session.costUsd)}</Text>
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

  // `none` means reasoning is off — no point showing chrome for the
  // absence of a feature. Suppressed in compact mode so the row fits.
  const reasoningSeg =
    !compact && session.reasoning && session.reasoning !== "none" ? (
      <Text color={theme.faint}>
        {theme.glyph.separator}
        {session.reasoning}
      </Text>
    ) : null;

  const durationSeg =
    !compact && session.sessionStartedAt ? (
      <>
        <Text>{theme.glyph.separator}</Text>
        <ElapsedTimer startedAt={session.sessionStartedAt} />
      </>
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

      {/* Right: ctx · cost · agents · mode. In narrow mode the parent
          stacks vertically, so add a top margin so the rows don't fuse. */}
      <Box marginTop={narrow ? 1 : 0}>
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
        {durationSeg}
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

function ElapsedTimer({startedAt}: {startedAt: number}) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const tick = () => setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    tick();
    const interval = setInterval(tick, 30000);
    return () => clearInterval(interval);
  }, [startedAt]);

  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  return (
    <Text color={theme.faint}>
      {m}:{String(s).padStart(2, "0")}
    </Text>
  );
}
