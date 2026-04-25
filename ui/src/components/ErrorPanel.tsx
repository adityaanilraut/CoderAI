import React, {useState} from "react";
import {Box, Text, useInput, useStdout} from "ink";
import {theme} from "../theme.js";
import {Rail, MessageHeader, Kbd} from "./Primitives.js";

export interface ErrorPanelProps {
  category: "provider" | "tool" | "internal" | "protocol";
  message: string;
  hint?: string;
  details?: string;
  /**
   * When true, the component listens for its detail-toggle shortcut.
   * The parent should set this only for the most recently-emitted
   * error so multiple panels do not compete for the same keys.
   */
  canExpand?: boolean;
  /** When true, require Ctrl+D so the compose box can stay focused safely. */
  promptActive?: boolean;
}

const FALLBACK_HINT =
  "Check stderr for more detail or try again.";

/**
 * Friendly error block.
 *
 *   ▌ ⚠ Provider error                  provider
 *   ▌ missing ANTHROPIC_API_KEY
 *   ▌ → run `coderAI setup` or set the env var
 *   ▌ Ctrl+D to show details
 *
 * Replaces raw Python tracebacks with a short summary, the hint supplied by
 * the agent over the protocol (UI no longer infers from message text), and an
 * optional expandable details view keyed on `d`/`Ctrl+D`.
 */
export function ErrorPanel({
  category,
  message,
  hint,
  details,
  canExpand = false,
  promptActive = false,
}: ErrorPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const {stdout} = useStdout();
  const columns = stdout?.columns ?? 100;
  const narrow = columns < theme.layout.narrowCols;

  useInput(
    (input, key) => {
      const wantsToggle = promptActive
        ? key.ctrl && (input === "d" || input === "D")
        : input === "d" || input === "D" || (key.ctrl && input === "d");
      if (wantsToggle) setExpanded((e) => !e);
    },
    {isActive: canExpand && Boolean(details)},
  );

  const title =
    category === "provider"
      ? "Provider error"
      : category === "tool"
        ? "Tool error"
        : category === "protocol"
          ? "Protocol error"
          : "Internal error";

  const resolvedHint = hint ?? FALLBACK_HINT;

  return (
    <Rail color={theme.danger} gap={narrow ? 1 : 2} marginBottom={1} marginTop={1}>
      <MessageHeader
        label={`${theme.glyph.warn} ${title}`}
        labelColor={theme.danger}
        right={narrow ? null : <Text color={theme.faint}>{category}</Text>}
      />
      <Box marginTop={1}>
        <Text color={theme.text}>{message}</Text>
      </Box>
      <Box>
        <Text color={theme.warning}>
          {theme.glyph.arrowRun} {resolvedHint}
        </Text>
      </Box>
      {details ? (
        <>
          {canExpand ? (
            <Box marginTop={1}>
              <Text color={theme.faint}>
                <Kbd label={promptActive ? "Ctrl+D" : "d"} />
                <Text color={theme.faint}>
                  {" "}
                  to {expanded ? "hide" : "show"} details
                </Text>
              </Text>
            </Box>
          ) : null}
          {expanded ? (
            <Box flexDirection="column" marginTop={1}>
              <Text color={theme.muted}>{details}</Text>
            </Box>
          ) : null}
        </>
      ) : null}
    </Rail>
  );
}
