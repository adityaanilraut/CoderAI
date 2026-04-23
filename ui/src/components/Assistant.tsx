import React from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";

export function Assistant({
  content,
  streaming,
  reasoning,
}: {
  content: string;
  streaming: boolean;
  reasoning: string;
}) {
  const trimmedReasoning = reasoning.trim();
  return (
    <Box flexDirection="column" marginTop={1} marginBottom={1} paddingLeft={1}>
      <Box>
        <Text color={theme.accent}>│ </Text>
        <Text color={theme.accent} bold>
          {streaming ? <Spinner type="dots" /> : "●"} Assistant
        </Text>
        {streaming ? <Text color={theme.muted}> · streaming…</Text> : null}
      </Box>
      {trimmedReasoning ? (
        <Box paddingLeft={2} marginTop={1}>
          <Text color={theme.muted} italic>
            {trimmedReasoning}
          </Text>
        </Box>
      ) : null}
      <Box paddingLeft={2} marginTop={1}>
        <Text color={theme.text}>{content}</Text>
      </Box>
    </Box>
  );
}

export function UserBubble({text}: {text: string}) {
  return (
    <Box flexDirection="column" marginTop={1} paddingLeft={1}>
      <Box>
        <Text color={theme.info}>│ </Text>
        <Text color={theme.success} bold>▸ You</Text>
      </Box>
      <Box paddingLeft={2} marginTop={1}>
        <Text color={theme.text}>{text}</Text>
      </Box>
    </Box>
  );
}
