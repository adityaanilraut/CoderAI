import React, {useState} from "react";
import {Box, Text, useInput} from "ink";
import {
  HELP_CLI_FOOTER,
  HELP_MENU_ENTRIES,
  type HelpMenuEntry,
} from "../helpMenu.js";
import {theme} from "../theme.js";
import {truncateSmart} from "../lib/format.js";

export interface HelpMenuProps {
  onPick: (slash: string) => void;
  onClose: () => void;
  maxWidth: number;
}

/**
 * /command picker overlay with keyboard navigation.
 *
 * Redesign: keeps the single round border (this is a modal and earns
 * chrome), but swaps the row treatment to a filled selection bar that
 * reads as a real focused menu row rather than a coloured text run.
 */
export function HelpMenu({onPick, onClose, maxWidth}: HelpMenuProps) {
  const [index, setIndex] = useState(0);
  const [showFooter, setShowFooter] = useState(false);
  const items = HELP_MENU_ENTRIES;

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (input === "?") {
        setShowFooter((v) => !v);
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

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const labelW = Math.min(34, Math.max(20, Math.floor(inner * 0.42)));
  const descW = Math.max(10, inner - labelW - 4);

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={theme.accentDim}
      paddingX={1}
      marginBottom={1}
    >
      {/* Header */}
      <Box marginBottom={1} justifyContent="space-between">
        <Text color={theme.accent} bold>
          {theme.glyph.diamond} Commands
        </Text>
        <Text color={theme.faint}>
          ↑↓ select
          {theme.glyph.separator}↵ run
          {theme.glyph.separator}? cli help
          {theme.glyph.separator}esc close
        </Text>
      </Box>

      {/* Entries */}
      {items.map((row, i) => (
        <HelpRow
          key={row.slash}
          row={row}
          selected={i === index}
          labelW={labelW}
          descW={descW}
        />
      ))}

      {/* Footer — only shown after the user presses `?` so the menu
          stays compact for the common case. */}
      {showFooter ? (
        <Box marginTop={1}>
          <Text color={theme.faint} dimColor>
            {truncateSmart(HELP_CLI_FOOTER, inner)}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

function HelpRow({row, selected, labelW, descW}: HelpRowProps) {
  const mark = selected ? theme.glyph.caret + " " : "  ";
  const lab = truncateSmart(row.slash, labelW);
  const rest = truncateSmart(row.desc, descW);
  return (
    <Box>
      <Text color={selected ? theme.accent : theme.faint}>{mark}</Text>
      <Text
        bold={selected}
        color={selected ? "black" : theme.info}
        backgroundColor={selected ? theme.accent : undefined}
      >
        {" "}
        {lab.padEnd(labelW - 1)}
      </Text>
      <Text color={selected ? theme.textSoft : theme.muted}> {rest}</Text>
    </Box>
  );
}

export interface HelpRowProps {
  row: HelpMenuEntry;
  selected: boolean;
  labelW: number;
  descW: number;
}
