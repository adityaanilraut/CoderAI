import React from "react";
import { Box, Text } from "ink";
import Spinner from "ink-spinner";
import { theme } from "../theme.js";
import { Rail, MessageHeader } from "./Primitives.js";

export interface AssistantProps {
  content: string;
  streaming: boolean;
  reasoning: string;
}

/**
 * Assistant message — rail-based.
 *
 *   ▌ ◆ CoderAI  streaming…
 *   ▌   reasoning (dim italic)
 *   ▌
 *   ▌ the actual content…
 *
 * The rail carries CoderAI's colour identity; the diamond glyph gives
 * the assistant a consistent visual anchor without a full border box.
 */
export function Assistant({ content, streaming, reasoning }: AssistantProps) {
  const trimmedReasoning = reasoning.trim();
  const label = `${theme.glyph.diamond} CoderAI`;

  return (
    <Rail color={theme.role.assistant} gap={2} marginBottom={1} marginTop={1}>
      <MessageHeader
        label={label}
        labelColor={theme.role.assistant}
        right={
          streaming ? (
            <Box>
              <Text color={theme.accent}>
                <Spinner type="dots" />
              </Text>
              <Text color={theme.muted}>  streaming</Text>
            </Box>
          ) : null
        }
      />
      {trimmedReasoning ? (
        <Box marginTop={1}>
          <Text color={theme.faint} italic>
            {trimmedReasoning}
          </Text>
        </Box>
      ) : null}
      {content ? (
        <Box marginTop={1}>
          <Text color={theme.text}>{content}</Text>
        </Box>
      ) : null}
    </Rail>
  );
}

export interface UserBubbleProps {
  text: string;
}

/**
 * User message — rail-based with a distinct green identity.
 *
 *   ▌ You
 *   ▌ the message they sent
 */
export function UserBubble({ text }: UserBubbleProps) {
  return (
    <Rail color={theme.role.user} gap={2} marginBottom={1} marginTop={1}>
      <MessageHeader label="You" labelColor={theme.role.user} />
      <Box marginTop={1}>
        <Text color={theme.textSoft}>{text}</Text>
      </Box>
    </Rail>
  );
}
