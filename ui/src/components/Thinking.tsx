import React, {useEffect, useState} from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";

export interface ThinkingProps {
  active: boolean;
  /**
   * Short description of *what* is being worked on right now (sub-agent
   * name, tool name, or current task). Surfacing this turns the spinner
   * from a vague "still alive" cue into actionable context.
   */
  detail?: string;
}

/**
 * Live "thinking" indicator shown below the timeline while the LLM
 * reasons before its next response.
 *
 *   ⠋ thinking · code-reviewer   12s    esc cancel
 */
export function Thinking({active, detail}: ThinkingProps) {
  const [ms, setMs] = useState(0);

  useEffect(() => {
    if (!active) {
      setMs(0);
      return;
    }
    const start = Date.now();
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
      {detail ? (
        <Text color={theme.muted}>
          {"  "}
          {theme.glyph.dot} {detail}
        </Text>
      ) : null}
      <Text color={theme.faint}>
        {"  "}
        {Math.floor(ms / 1000)}s
      </Text>
      <Text color={theme.faint}>{"    "}esc cancel</Text>
    </Box>
  );
}
