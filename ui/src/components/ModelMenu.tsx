import React, {useState, useMemo} from "react";
import {Box, Text, useInput} from "ink";
import Spinner from "ink-spinner";
import {theme} from "../theme.js";
import {truncateSmart} from "../lib/format.js";

export interface ModelMenuProps {
  /** Provider -> model names, e.g. {"Anthropic": ["opus", "sonnet", ...], ...} */
  models: Record<string, string[]> | null;
  /** Currently active model so we can highlight it. */
  current: string;
  onPick: (model: string) => void;
  onClose: () => void;
  maxWidth: number;
}

interface FlatEntry {
  model: string;
  provider: string;
}

/**
 * Navigable model picker overlay. Lists every model grouped by provider;
 * arrow keys move, Enter selects, Esc closes.
 *
 * Styled to match HelpMenu so the interaction feels consistent.
 */
export function ModelMenu({models, current, onPick, onClose, maxWidth}: ModelMenuProps) {
  const flat: FlatEntry[] = useMemo(() => {
    if (!models) return [];
    const out: FlatEntry[] = [];
    for (const [provider, modelList] of Object.entries(models)) {
      for (const m of modelList) {
        out.push({model: m, provider});
      }
    }
    return out;
  }, [models]);

  const [index, setIndex] = useState(() => {
    const pos = flat.findIndex((e) => e.model === current);
    return pos >= 0 ? pos : 0;
  });

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (key.upArrow) {
        setIndex((i) => (i - 1 + flat.length) % flat.length);
        return;
      }
      if (key.downArrow) {
        setIndex((i) => (i + 1) % flat.length);
        return;
      }
      if (key.return) {
        const entry = flat[index];
        if (entry) onPick(entry.model);
      }
    },
    {isActive: true},
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const modelW = Math.min(36, Math.max(22, Math.floor(inner * 0.45)));

  // Group entries so we can render provider headings.
  const groups: {provider: string; entries: {model: string; idx: number}[]}[] = useMemo(() => {
    if (!models) return [];
    const out: {provider: string; entries: {model: string; idx: number}[]}[] = [];
    for (const [provider, modelList] of Object.entries(models)) {
      const entries = modelList.map((m) => ({model: m, idx: flat.findIndex((e) => e.model === m && e.provider === provider)}));
      out.push({provider, entries});
    }
    return out;
  }, [models, flat]);

  const loading = !models;

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
          {theme.glyph.diamond} Switch Model
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

      {loading ? (
        <Box>
          <Text color={theme.accent}><Spinner type="dots" /></Text>
          <Text color={theme.faint}> loading models…</Text>
        </Box>
      ) : groups.length === 0 ? (
        <Text color={theme.muted}>No models available</Text>
      ) : (
        /* Grouped model list */
        groups.map((group) => (
          <Box key={group.provider} flexDirection="column" marginBottom={1}>
            <Text color={theme.faint} dimColor>
              {group.provider}
            </Text>
            {group.entries.map(({model, idx}) => {
              const selected = idx === index;
              const isCurrent = model === current;
              const mark = selected ? theme.glyph.caret + " " : "  ";
              const lab = truncateSmart(model, modelW);
              return (
                <Box key={model}>
                  <Text color={selected ? theme.accent : theme.faint}>{mark}</Text>
                  <Text
                    bold={selected}
                    color={selected ? "black" : theme.info}
                    backgroundColor={selected ? theme.accent : undefined}
                  >
                    {" "}
                    {lab.padEnd(modelW - 1)}
                  </Text>
                  <Text color={selected ? theme.textSoft : theme.muted}>
                    {" "}
                    {isCurrent ? "(current)" : ""}
                  </Text>
                </Box>
              );
            })}
          </Box>
        ))
      )}
    </Box>
  );
}
