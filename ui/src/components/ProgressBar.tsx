import React from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";

export interface ProgressBarProps {
  label: string;
  current?: number;
  total?: number;
  kind: "tokens" | "files" | "steps";
}

/**
 * Inline progress indicator shown during long-running operations
 * (batch file edits, downloads, context compaction, etc.).
 *
 *   ⠋ Processing files  ████████░░  8/10
 */
export function ProgressBar({label, current, total, kind}: ProgressBarProps) {
  const hasTotal = typeof total === "number" && total > 0;
  const pct = hasTotal && typeof current === "number" ? Math.min(1, Math.max(0, current / total)) : null;
  const bar = pct !== null ? renderMeter(pct) : null;
  const unit = kind === "tokens" ? "tok" : kind === "files" ? "files" : "steps";

  return (
    <Box paddingLeft={theme.spacing.md} marginBottom={theme.spacing.sm}>
      <Text color={theme.accent}>
        {pct === null ? "⠋" : pct >= 1 ? theme.glyph.tick : "⠋"}{" "}
      </Text>
      <Text color={theme.textSoft}>{label}</Text>
      {bar ? (
        <>
          <Text>  </Text>
          {bar}
        </>
      ) : null}
      {hasTotal ? (
        <Text color={theme.faint}>
          {"  "}
          {typeof current === "number" ? current : 0}/{total} {unit}
        </Text>
      ) : typeof current === "number" ? (
        <Text color={theme.faint}>
          {"  "}
          {current} {unit}
        </Text>
      ) : null}
    </Box>
  );
}

function renderMeter(pct: number) {
  const WIDTH = 10;
  const filled = Math.max(0, Math.min(WIDTH, Math.round(pct * WIDTH)));
  const empty = WIDTH - filled;
  return (
    <Box>
      <Text color={theme.accent}>{"█".repeat(filled)}</Text>
      <Text color={theme.faint}>{"░".repeat(empty)}</Text>
    </Box>
  );
}
