import React from "react";
import {Box, Text, useStdout} from "ink";
import {theme} from "../theme.js";

export interface SeparatorProps {
  message: string;
}

/**
 * Horizontal rule used when older history is trimmed from the timeline.
 * Visually distinct from `Toast` so the user immediately reads it as
 * "context was dropped" rather than a transient notice — toasts otherwise
 * scroll past unnoticed.
 *
 *   ──────  142 earlier entries trimmed · keeping most recent 400  ──────
 */
export function Separator({message}: SeparatorProps) {
  const {stdout} = useStdout();
  const columns = stdout?.columns ?? 100;
  const labelLen = message.length + 2; // padding around the label
  const sideLen = Math.max(4, Math.floor((columns - labelLen) / 2) - 2);
  const side = "─".repeat(sideLen);
  return (
    <Box marginTop={1} marginBottom={1} paddingX={2}>
      <Text color={theme.faint}>
        {side}  <Text color={theme.warning}>{message}</Text>  {side}
      </Text>
    </Box>
  );
}
