import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";

export interface AssistantProps {
  content: string;
  streaming: boolean;
  reasoning: string;
  showReasoning?: boolean;
}

/**
 * Assistant message — chrome-free.
 *
 * The model talking is signaled by the absence of `❯`. No header row,
 * no rail, no role label. Reasoning is buffered and only rendered when
 * verbose mode is on (or when the user pressed Ctrl+R / `/think`, which
 * surfaces it as an explicit toast — see useAgent.revealReasoning).
 */
export function Assistant({ content, streaming, reasoning, showReasoning }: AssistantProps) {
  const trimmedReasoning = reasoning.trim();
  const hasContent = Boolean(content);
  const hasReasoning = Boolean(trimmedReasoning) && showReasoning;
  if (!hasContent && !hasReasoning && !streaming) return null;

  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingX={2}>
      {hasReasoning ? (
        <Box marginBottom={hasContent || streaming ? 1 : 0}>
          <Text color={theme.faint} italic>
            {trimmedReasoning}
          </Text>
        </Box>
      ) : null}
      {hasContent ? (
        <Text color={theme.text}>
          {content}
          {streaming ? <Text color={theme.faint}> ▋</Text> : null}
        </Text>
      ) : streaming ? (
        <Text color={theme.faint}>…</Text>
      ) : null}
    </Box>
  );
}

export interface UserBubbleProps {
  text: string;
}

/**
 * User message echoed back into the transcript. Uses `sentCaret` (›) to
 * distinguish a *sent* message from the *editable* prompt below, which uses
 * `caret` (❯). Without this differentiation a glanced page can't separate
 * "I said this" from "now editing".
 */
export function UserBubble({ text }: UserBubbleProps) {
  const lines = text.split("\n");
  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingX={1}>
      {lines.map((line, i) => (
        <Box key={i}>
          {i === 0 ? (
            <Text color={theme.role.user} bold>
              {theme.glyph.sentCaret}{" "}
            </Text>
          ) : (
            <Text>{"  "}</Text>
          )}
          <Text color={theme.textSoft}>{line}</Text>
        </Box>
      ))}
    </Box>
  );
}
