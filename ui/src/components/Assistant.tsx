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
    <Box
      flexDirection="column"
      marginBottom={1}
      borderStyle="round"
      borderColor={streaming ? theme.accent : theme.border}
      paddingX={1}
    >
      <Box justifyContent="space-between">
        <Text color={theme.accent} bold>
          {streaming ? <Spinner type="dots" /> : "●"} Assistant
        </Text>
        <Text color={theme.muted}>response</Text>
      </Box>

      {trimmedReasoning ? (
        <Box
          marginTop={1}
          borderStyle="single"
          borderColor={theme.borderSoft}
          paddingX={1}
          flexDirection="column"
        >
          <Text color={theme.info} bold>
            Working Notes
          </Text>
          <Text color={theme.muted} italic>
            {trimmedReasoning}
          </Text>
        </Box>
      ) : null}

      <Box marginTop={1}>
        <Text color={theme.text}>{content}</Text>
      </Box>
    </Box>
  );
}

export function UserBubble({text}: {text: string}) {
  return (
    <Box
      flexDirection="column"
      marginBottom={1}
      borderStyle="round"
      borderColor={theme.info}
      paddingX={1}
    >
      <Box justifyContent="space-between">
        <Text color={theme.success} bold>
          ▸ You
        </Text>
        <Text color={theme.muted}>prompt</Text>
      </Box>
      <Box marginTop={1}>
        <Text color={theme.text}>{text}</Text>
      </Box>
    </Box>
  );
}
