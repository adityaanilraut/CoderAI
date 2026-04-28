import React, {useState} from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import type {ReasoningEffort} from "../protocol.js";

const OPTIONS: {value: ReasoningEffort; desc: string}[] = [
  {value: "high", desc: "Deepest reasoning for complex problems"},
  {value: "medium", desc: "Balanced reasoning (default)"},
  {value: "low", desc: "Minimal reasoning for simple tasks"},
  {value: "none", desc: "No reasoning — fastest responses"},
];

export interface ReasoningMenuProps {
  current: ReasoningEffort;
  onPick: (effort: ReasoningEffort) => void;
  onClose: () => void;
  maxWidth: number;
}

export function ReasoningMenu({current, onPick, onClose, maxWidth}: ReasoningMenuProps) {
  const [index, setIndex] = useState(() => {
    const pos = OPTIONS.findIndex((o) => o.value === current);
    return pos >= 0 ? pos : 1; // default to medium
  });

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (key.upArrow) {
        setIndex((i) => (i - 1 + OPTIONS.length) % OPTIONS.length);
        return;
      }
      if (key.downArrow) {
        setIndex((i) => (i + 1) % OPTIONS.length);
        return;
      }
      if (key.return) {
        const opt = OPTIONS[index];
        if (opt) onPick(opt.value);
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 76));
  const labelW = Math.min(16, Math.floor(inner * 0.28));
  const descW = Math.max(10, inner - labelW - 4);

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
          {theme.glyph.diamond} Reasoning Effort
          <Text color={theme.faint}>
            {"  "}{theme.glyph.dot} current: <Text color={theme.muted}>{current}</Text>
          </Text>
        </Text>
        <Text color={theme.faint}>
          ↑↓ select
          {theme.glyph.separator}↵ set
          {theme.glyph.separator}esc close
        </Text>
      </Box>

      {OPTIONS.map((opt, i) => {
        const selected = i === index;
        const isCurrent = opt.value === current;
        const mark = selected ? theme.glyph.caret + " " : "  ";
        return (
          <Box key={opt.value}>
            <Text color={selected ? theme.accent : theme.faint}>{mark}</Text>
            <Text
              bold={selected}
              color={selected ? "black" : theme.info}
              backgroundColor={selected ? theme.accent : undefined}
            >
              {" "}
              {opt.value.padEnd(labelW - 1)}
            </Text>
            <Text color={selected ? theme.textSoft : theme.muted}>
              {" "}
              {opt.desc}
              {isCurrent ? "  (current)" : ""}
            </Text>
          </Box>
        );
      })}
    </Box>
  );
}
