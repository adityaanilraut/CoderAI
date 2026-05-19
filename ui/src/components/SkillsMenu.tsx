import React, {useState, useEffect} from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import {truncateSmart} from "../lib/format.js";
import {QuietSpinner} from "./QuietSpinner.js";

export interface SkillsMenuProps {
  skills: {name: string; description: string}[] | null;
  onPick: (skill: string) => void;
  onClose: () => void;
  maxWidth: number;
}

export function SkillsMenu({skills, onPick, onClose, maxWidth}: SkillsMenuProps) {
  const [index, setIndex] = useState(0);

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (!skills || skills.length === 0) return;
      if (key.upArrow) {
        setIndex((i) => (i - 1 + skills.length) % skills.length);
        return;
      }
      if (key.downArrow) {
        setIndex((i) => (i + 1) % skills.length);
        return;
      }
      if (key.return) {
        onPick(skills[index].name);
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const nameW = Math.min(24, Math.max(16, Math.floor(inner * 0.3)));
  const descW = inner - nameW - 4;

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
          {theme.glyph.diamond} Project Skills
        </Text>
        <Text color={theme.faint}>
          ↑↓ select
          {theme.glyph.separator}↵ insert
          {theme.glyph.separator}esc close
        </Text>
      </Box>

      {!skills ? (
        <Box>
          <Text color={theme.accent}><QuietSpinner /></Text>
          <Text color={theme.faint}> loading skills…</Text>
        </Box>
      ) : skills.length === 0 ? (
        <Text color={theme.muted}>No skills available</Text>
      ) : (
        <Box flexDirection="column" marginBottom={1}>
          {skills.map((skill, idx) => {
            const selected = idx === index;
            const mark = selected ? theme.glyph.caret + " " : "  ";
            const name = truncateSmart(skill.name, nameW);
            const desc = truncateSmart(skill.description.replace(/\n/g, " "), descW);
            return (
              <Box key={skill.name}>
                <Text color={selected ? theme.accent : theme.faint}>{mark}</Text>
                <Text
                  bold={selected}
                  color={selected ? theme.bg : theme.info}
                  backgroundColor={selected ? theme.accent : undefined}
                >
                  {" "}
                  {name.padEnd(nameW - 1)}
                </Text>
                <Text color={selected ? theme.textSoft : theme.muted}>
                  {" "}
                  {desc}
                </Text>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
}
