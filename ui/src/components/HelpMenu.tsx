import React, {useState} from "react";
import {Box, Text, useInput} from "ink";
import {
  HELP_CLI_FOOTER,
  HELP_MENU_ENTRIES,
  type HelpMenuEntry,
} from "../helpMenu.js";
import {theme} from "../theme.js";

function truncate(s: string, max: number): string {
  if (max < 8) return "…";
  return s.length <= max ? s : s.slice(0, Math.max(0, max - 1)) + "…";
}

export function HelpMenu({
  onPick,
  onClose,
  maxWidth,
}: {
  onPick: (slash: string) => void;
  onClose: () => void;
  maxWidth: number;
}) {
  const [index, setIndex] = useState(0);
  const items = HELP_MENU_ENTRIES;

  useInput(
    (_input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (key.upArrow) {
        setIndex((i) => (i - 1 + items.length) % items.length);
        return;
      }
      if (key.downArrow) {
        setIndex((i) => (i + 1) % items.length);
        return;
      }
      if (key.return) {
        const row = items[index];
        if (row) onPick(row.slash);
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 2, 96));
  const labelW = Math.min(34, Math.max(20, Math.floor(inner * 0.42)));
  const descW = Math.max(10, inner - labelW - 4);

  return (
    <Box
      flexDirection="column"
      borderStyle="double"
      borderColor={theme.accentDim}
      paddingX={1}
      marginBottom={1}
    >
      <Box marginBottom={1}>
        <Text color={theme.accent} bold>
          Commands
        </Text>
        <Text color={theme.muted}>
          {" "}
          · command palette · ↑↓ select · Enter run · Esc close
        </Text>
      </Box>
      {items.map((row, i) => (
        <HelpRow
          key={row.slash}
          row={row}
          selected={i === index}
          labelW={labelW}
          descW={descW}
        />
      ))}
      <Box marginTop={1} flexDirection="column">
        <Text color={theme.muted} dimColor>
          {truncate(HELP_CLI_FOOTER, inner)}
        </Text>
      </Box>
    </Box>
  );
}

function HelpRow({
  row,
  selected,
  labelW,
  descW,
}: {
  row: HelpMenuEntry;
  selected: boolean;
  labelW: number;
  descW: number;
}) {
  const mark = selected ? "❯ " : "  ";
  const lab = truncate(row.slash, labelW);
  const rest = truncate(row.desc, descW);
  return (
    <Box>
      <Text color={selected ? theme.accent : theme.muted}>{mark}</Text>
      <Text
        bold={selected}
        color={selected ? theme.focus : theme.info}
        backgroundColor={selected ? theme.accentDim : undefined}
      >
        {lab.padEnd(labelW)}
      </Text>
      <Text color={theme.muted}> {rest}</Text>
    </Box>
  );
}
