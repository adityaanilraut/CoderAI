import React, {useState, useEffect} from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import {truncateSmart} from "../lib/format.js";
import {QuietSpinner} from "./QuietSpinner.js";

export interface PersonaMenuProps {
  personas: string[] | null;
  current: string | null;
  onPick: (persona: string) => void;
  onClose: () => void;
  maxWidth: number;
}

export function PersonaMenu({personas, current, onPick, onClose, maxWidth}: PersonaMenuProps) {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (personas && personas.length > 0) {
      const pos = personas.indexOf(current || "default");
      setIndex(pos >= 0 ? pos : 0);
    }
  }, [personas, current]);

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (!personas || personas.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i - 1 + personas.length) % personas.length);
        return;
      }
      if (key.downArrow) {
        setIndex((i) => (i + 1) % personas.length);
        return;
      }
      if (key.return) {
        onPick(personas[index]);
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const itemW = Math.min(36, Math.max(22, Math.floor(inner * 0.45)));

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
          {theme.glyph.diamond} Switch Persona
          {current ? (
            <Text color={theme.faint}>
              {"  "}{theme.glyph.dot} current: <Text color={theme.muted}>{current}</Text>
            </Text>
          ) : null}
        </Text>
        <Text color={theme.faint}>
          ↑↓ select
          {theme.glyph.separator}↵ switch
          {theme.glyph.separator}esc close
        </Text>
      </Box>

      {!personas ? (
        <Box>
          <Text color={theme.accent}><QuietSpinner /></Text>
          <Text color={theme.faint}> loading personas…</Text>
        </Box>
      ) : personas.length === 0 ? (
        <Text color={theme.muted}>No personas available</Text>
      ) : (
        <Box flexDirection="column" marginBottom={1}>
          {personas.map((persona, idx) => {
            const selected = idx === index;
            const isCurrent = persona === (current || "default");
            const mark = selected ? theme.glyph.caret + " " : "  ";
            const lab = truncateSmart(persona, itemW);
            return (
              <Box key={persona}>
                <Text color={selected ? theme.accent : theme.faint}>{mark}</Text>
                <Text
                  bold={selected}
                  color={selected ? theme.bg : theme.info}
                  backgroundColor={selected ? theme.accent : undefined}
                >
                  {" "}
                  {lab.padEnd(itemW - 1)}
                </Text>
                <Text color={selected ? theme.textSoft : theme.muted}>
                  {" "}
                  {isCurrent ? "(current)" : ""}
                </Text>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
