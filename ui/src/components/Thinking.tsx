import React, {useEffect, useState} from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";

export interface ThinkingProps {
  active: boolean;
}

/**
 * Live "thinking" indicator shown below the timeline while the LLM
 * reasons before its next response.
 *
 *   ⠋ thinking  12s       esc to interrupt
 *
 * Redesign: slightly indented to sit below the chat rail column, uses
 * `faint` for the hint so it fades into the background when the model
 * is fast and the message flashes briefly.
 */
export function Thinking({active}: ThinkingProps) {
  const [ms, setMs] = useState(0);

  useEffect(() => {
    if (!active) {
      setMs(0);
      return;
    }
    const start = Date.now();
    // 1s ticks — higher frequencies cause Ink live-region redraws that
    // can scroll long transcripts to the top.
    const interval = setInterval(() => setMs(Date.now() - start), 1000);
    return () => clearInterval(interval);
  }, [active]);

  if (!active) return null;

  return (
    <Box paddingLeft={theme.spacing.md} marginBottom={theme.spacing.sm}>
      <Text color={theme.accent}>
        <Spinner type="dots" />
      </Text>
      <Text color={theme.textSoft}> thinking</Text>
      <Text color={theme.faint}>
        {"  "}
        {Math.floor(ms / 1000)}s
      </Text>
      <Text color={theme.faint}>
        {"    "}
        esc to interrupt
      </Text>
    </Box>
  );
}
