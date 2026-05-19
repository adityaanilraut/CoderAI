import React from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import {truncateSmart} from "../lib/format.js";
import {QuietSpinner} from "./QuietSpinner.js";

export interface ContextOverlayProps {
  files: {path: string; size: number}[] | null;
  onClose: () => void;
  maxWidth: number;
}

export function ContextOverlay({files, onClose, maxWidth}: ContextOverlayProps) {
  useInput(
    (input, key) => {
      if (key.escape || key.return) {
        onClose();
        return;
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const pathW = Math.max(20, inner - 15);

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    return `${(bytes / 1024).toFixed(1)} KB`;
  };

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={theme.accentDim}
      paddingX={1}
      marginBottom={1}
    >
      <Box marginBottom={1} justifyContent="space-between">
        <Text color={theme.accent} bold>
          {theme.glyph.diamond} Pinned Context
        </Text>
        <Text color={theme.faint}>
          esc/↵ close
        </Text>
      </Box>

      {!files ? (
        <Box>
          <Text color={theme.accent}><QuietSpinner /></Text>
          <Text color={theme.faint}> loading context files…</Text>
        </Box>
      ) : files.length === 0 ? (
        <Text color={theme.muted}>No files pinned to context</Text>
      ) : (
        <Box flexDirection="column" marginBottom={1}>
          {files.map((file) => {
            const p = truncateSmart(file.path, pathW);
            return (
              <Box key={file.path} justifyContent="space-between">
                <Text color={theme.text}>  {p}</Text>
                <Text color={theme.faint}>{formatSize(file.size)}</Text>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
