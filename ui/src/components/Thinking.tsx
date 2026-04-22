import React, {useEffect, useState} from "react";
import {Box, Text} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";

/** Spinner + live elapsed timer shown while the LLM is reasoning. */
export function Thinking({active}: {active: boolean}) {
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
    <Box
      borderStyle="round"
      borderColor={theme.borderSoft}
      paddingX={1}
      marginBottom={1}
    >
      <Text color={theme.accent}>
        <Spinner type="dots" />
      </Text>
      <Text color={theme.muted}>
        {" "}
        reasoning in progress · {(ms / 1000).toFixed(1)}s{" "}
        <Text dimColor>(Esc to interrupt)</Text>
      </Text>
    </Box>
  );
}
