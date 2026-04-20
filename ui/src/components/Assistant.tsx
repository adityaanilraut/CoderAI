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
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box>
        <Text color={theme.accent} bold>
          {streaming ? <Spinner type="dots" /> : "●"} Assistant
        </Text>
      </Box>

      {reasoning ? (
        <Box
          borderStyle="single"
          borderColor={theme.accentDim}
          paddingX={1}
          marginY={0}
        >
          <Text color={theme.muted} italic>
            {reasoning.trim()}
          </Text>
        </Box>
      ) : null}

      <Box marginTop={0}>
        <Text>{content}</Text>
      </Box>
    </Box>
  );
}

export function UserBubble({text}: {text: string}) {
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box>
        <Text color={theme.success} bold>
          ▸ You
        </Text>
      </Box>
      <Box>
        <Text>{text}</Text>
      </Box>
    </Box>
  );
}
