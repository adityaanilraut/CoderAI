import React, { useMemo, useState } from "react";
import { Box, Text, useInput } from "ink";
import { theme } from "../theme.js";
import type { TimelineItem } from "../hooks/useAgent.js";
import { truncateSmart } from "../lib/format.js";

export interface SearchOverlayProps {
  timeline: TimelineItem[];
  filter: string;
  onFilterChange: (f: string) => void;
  onClose: () => void;
  maxWidth: number;
}

interface SearchHit {
  index: number;
  kind: string;
  preview: string;
}

export function SearchOverlay({
  timeline,
  filter,
  onFilterChange,
  onClose,
  maxWidth,
}: SearchOverlayProps) {
  const [selectedIdx, setSelectedIdx] = useState(0);

  const hits = useMemo(() => {
    if (!filter) return [] as SearchHit[];
    const q = filter.toLowerCase();
    const results: SearchHit[] = [];
    for (let i = timeline.length - 1; i >= 0; i--) {
      const item = timeline[i];
      let text = "";
      switch (item.kind) {
        case "user":
          text = item.text;
          break;
        case "assistant":
          text = item.content + " " + item.reasoning;
          break;
        case "tool":
          text = item.name + " " + (item.preview || "") + " " + (item.error || "");
          break;
        case "diff":
          text = item.path + " " + item.diff;
          break;
        case "error":
          text = item.message + " " + (item.details || "");
          break;
        case "toast":
          text = item.message;
          break;
        case "approval":
          text = item.tool;
          break;
      }
      if (text.toLowerCase().includes(q)) {
        results.push({
          index: i,
          kind: item.kind,
          preview: summarizeHit(item, 80),
        });
      }
    }
    return results;
  }, [timeline, filter]);

  useInput(
    (input, key) => {
      if (key.escape) {
        onClose();
        return;
      }
      if (key.return) {
        onClose();
        return;
      }
      if (key.upArrow) {
        setSelectedIdx((i) =>
          hits.length > 1 ? (i - 1 + hits.length) % hits.length : 0,
        );
        return;
      }
      if (key.downArrow) {
        setSelectedIdx((i) =>
          hits.length > 1 ? (i + 1) % hits.length : 0,
        );
        return;
      }
      if (key.backspace || key.delete) {
        onFilterChange(filter.slice(0, -1));
        setSelectedIdx(0);
        return;
      }
      if (input.length === 1 && !key.ctrl && !key.meta) {
        onFilterChange(filter + input);
        setSelectedIdx(0);
      }
    },
    { isActive: true },
  );

  const inner = Math.max(40, Math.min(maxWidth - 4, 96));
  const previewW = Math.max(20, inner - 12);

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
          {theme.glyph.diamond} Search Transcript
        </Text>
        <Text color={theme.faint}>
          type to filter
          {theme.glyph.separator}↵ close
          {theme.glyph.separator}esc close
        </Text>
      </Box>

      <Box marginBottom={1}>
        <Text color={theme.info}>
          Search:{" "}
          <Text color={theme.textSoft}>
            {filter || "(start typing to filter)"}
          </Text>
          {filter ? (
            <Text color={theme.faint}>
              {"  "}
              ({hits.length} hit{hits.length === 1 ? "" : "s"})
            </Text>
          ) : null}
        </Text>
      </Box>

      {hits.length === 0 && filter ? (
        <Text color={theme.muted}>No matches found.</Text>
      ) : (
        hits.slice(0, 20).map((hit, i) => {
          const selected = i === selectedIdx;
          const kindLabel = kindIcon(hit.kind);
          const preview = truncateSmart(hit.preview, previewW);
          return (
            <Box key={hit.index} flexDirection="column">
              <Box>
                <Text color={selected ? theme.accent : theme.faint}>
                  {selected ? theme.glyph.caret : " "}{" "}
                </Text>
                <Text color={selected ? theme.textSoft : theme.muted}>
                  {kindLabel}
                </Text>
                <Text
                  bold={selected}
                  color={selected ? theme.text : theme.textSoft}
                  backgroundColor={selected ? theme.accentDim : undefined}
                >
                  {" "}
                  {preview}
                </Text>
              </Box>
            </Box>
          );
        })
      )}

      {hits.length > 20 ? (
        <Text color={theme.faint}>
          … and {hits.length - 20} more matches
        </Text>
      ) : null}
    </Box>
  );
}

function kindIcon(kind: string): string {
  switch (kind) {
    case "user":
      return theme.glyph.caret;
    case "assistant":
      return theme.glyph.diamond;
    case "tool":
      return theme.glyph.cog;
    case "diff":
      return "±";
    case "error":
    case "approval":
      return theme.glyph.warn;
    case "toast":
      return theme.glyph.info;
    default:
      return theme.glyph.dot;
  }
}

function summarizeHit(item: TimelineItem, max: number): string {
  switch (item.kind) {
    case "user":
      return item.text.slice(0, max);
    case "assistant":
      return item.content.slice(0, max);
    case "tool":
      return `${item.name} ${item.preview || item.error || ""}`.slice(0, max);
    case "diff":
      return `${item.path}: ${item.diff.slice(0, max)}`;
    case "error":
      return item.message.slice(0, max);
    case "toast":
      return item.message.slice(0, max);
    case "approval":
      return item.tool;
  }
}
