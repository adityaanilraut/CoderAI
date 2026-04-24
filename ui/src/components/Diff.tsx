import React from "react";
import {Box, Text} from "ink";
import {theme} from "../theme.js";

export interface DiffProps {
  path: string;
  diff: string;
  /** Collapse runs of unchanged lines longer than 2 × this. */
  contextLines?: number;
  /**
   * Hard cap per-line rendering width. Anything longer is truncated with an
   * ellipsis so very long machine-generated lines don't blow out the
   * surrounding round border.
   */
  maxLineWidth?: number;
}

type LineKind = "add" | "del" | "ctx" | "hunk" | "meta";

interface ParsedLine {
  kind: LineKind;
  text: string;
  oldLn?: number;
  newLn?: number;
}

type RenderRow =
  | {kind: "line"; line: ParsedLine}
  | {kind: "elision"; count: number};

/**
 * Rich unified-diff renderer with gutter line numbers and colored
 * backgrounds. Uses a two-pass elision so leading/trailing unchanged
 * blocks collapse symmetrically around changes.
 */
export function Diff({
  path,
  diff,
  contextLines = 3,
  maxLineWidth = 200,
}: DiffProps) {
  const stats = countStats(diff);
  const parsed = parseDiff(diff);
  const rows = elide(parsed, contextLines);
  const cap = Math.max(40, maxLineWidth);

  return (
    <Box
      flexDirection="column"
      paddingLeft={theme.spacing.md}
      marginBottom={theme.spacing.sm}
      marginTop={theme.spacing.sm}
    >
      <Box>
        <Text color={theme.info} bold>
          ◈{" "}
        </Text>
        <Text bold color={theme.text}>
          {path}
        </Text>
        <Text color={theme.faint}>
          {"   "}
          <Text color={theme.success}>+{stats.adds}</Text>
          <Text color={theme.faint}> / </Text>
          <Text color={theme.danger}>-{stats.dels}</Text>
        </Text>
      </Box>

      <Box
        flexDirection="column"
        marginTop={1}
        paddingLeft={2}
      >
        {rows.map((row, i) => {
          if (row.kind === "elision") {
            return (
              <Box key={i}>
                <Text color={theme.muted}>{"    │     "}</Text>
                <Text color={theme.muted}>
                  {"  "}… {row.count} unchanged lines …
                </Text>
              </Box>
            );
          }
          const {line} = row;
          const text =
            line.text.length > cap ? line.text.slice(0, cap - 1) + "…" : line.text;
          return (
            <Box key={i}>
              <Text color={theme.muted}>
                {formatGutter(line.oldLn, line.newLn)}
              </Text>
              <Text
                color={colorFor(line.kind)}
                backgroundColor={bgFor(line.kind)}
              >
                {"  "}
                {text}
              </Text>
            </Box>
          );
        })}
      </Box>
    </Box>
  );
}

function countStats(diff: string): {adds: number; dels: number} {
  let adds = 0;
  let dels = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) adds++;
    else if (line.startsWith("-")) dels++;
  }
  return {adds, dels};
}

function parseDiff(diff: string): ParsedLine[] {
  const out: ParsedLine[] = [];
  let oldLn = 0;
  let newLn = 0;

  for (const raw of diff.split("\n")) {
    if (raw.startsWith("+++") || raw.startsWith("---")) {
      out.push({kind: "meta", text: raw});
      continue;
    }
    if (raw.startsWith("@@")) {
      const m = raw.match(/-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?/);
      if (m) {
        oldLn = Number(m[1]);
        newLn = Number(m[2]);
      }
      out.push({kind: "hunk", text: raw});
      continue;
    }
    if (raw.startsWith("+")) {
      out.push({kind: "add", text: raw, newLn});
      newLn++;
      continue;
    }
    if (raw.startsWith("-")) {
      out.push({kind: "del", text: raw, oldLn});
      oldLn++;
      continue;
    }
    out.push({kind: "ctx", text: raw, oldLn, newLn});
    oldLn++;
    newLn++;
  }
  return out;
}

/**
 * Keep context lines within `contextLines` of any change; elide the rest.
 * Unlike a streaming collapse, this gives symmetric before/after context
 * and never shows trailing context that doesn't precede a change.
 */
function elide(lines: ParsedLine[], contextLines: number): RenderRow[] {
  const keep = new Array<boolean>(lines.length).fill(false);

  for (let i = 0; i < lines.length; i++) {
    const k = lines[i].kind;
    if (k === "add" || k === "del" || k === "hunk" || k === "meta") {
      keep[i] = true;
      for (let j = 1; j <= contextLines; j++) {
        if (i - j >= 0) keep[i - j] = true;
        if (i + j < lines.length) keep[i + j] = true;
      }
    }
  }

  const rows: RenderRow[] = [];
  let elided = 0;
  for (let i = 0; i < lines.length; i++) {
    if (keep[i]) {
      if (elided > 0) {
        rows.push({kind: "elision", count: elided});
        elided = 0;
      }
      rows.push({kind: "line", line: lines[i]});
    } else {
      elided++;
    }
  }
  // Drop any trailing all-context block entirely (no changes follow).
  return rows;
}

function colorFor(k: LineKind): string {
  switch (k) {
    case "add":
      return theme.success;
    case "del":
      return theme.danger;
    case "hunk":
      return theme.info;
    case "meta":
      return theme.muted;
    default:
      return theme.muted;
  }
}

function bgFor(k: LineKind): string | undefined {
  if (k === "add") return theme.diff.addBg;
  if (k === "del") return theme.diff.delBg;
  return undefined;
}

function formatGutter(oldLn?: number, newLn?: number): string {
  const o = oldLn != null ? String(oldLn).padStart(4, " ") : "    ";
  const n = newLn != null ? String(newLn).padStart(4, " ") : "    ";
  return `${o} │ ${n}`;
}
