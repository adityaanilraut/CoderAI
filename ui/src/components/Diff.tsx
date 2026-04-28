import React from "react";
import {Box, Text, useStdout} from "ink";
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
  verbose?: boolean;
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
  verbose = false,
}: DiffProps) {
  const {stdout} = useStdout();
  const columns = stdout?.columns ?? 100;
  const narrow = columns < theme.layout.narrowCols;
  const stats = countStats(diff);
  const parsed = parseDiff(diff);
  const rows = elide(parsed, contextLines);
  const cap = Math.max(40, maxLineWidth);

  if (!verbose) {
    // Compact summary plus a 3-line preview of the first +/- hunk so the
    // user can see the *shape* of the change without flipping verbose on.
    // Empty diffs (deletes only) gracefully degrade to the header line.
    const previewLines = firstChangePreview(parsed, 3);
    return (
      <Box flexDirection="column" paddingX={2}>
        <Box>
          <Text color={theme.info} bold>± </Text>
          <Text bold color={theme.text}>{path}</Text>
          <Text color={theme.faint}>
            {"   "}
            <Text color={theme.success}>+{stats.adds}</Text>
            {" "}
            <Text color={theme.danger}>-{stats.dels}</Text>
          </Text>
        </Box>
        {previewLines.length > 0 ? (
          <Box flexDirection="column" paddingLeft={4}>
            {previewLines.map((line, i) => (
              <Text
                key={i}
                color={colorFor(line.kind)}
                backgroundColor={bgFor(line.kind)}
              >
                {line.text.length > cap
                  ? line.text.slice(0, cap - 1) + "…"
                  : line.text}
              </Text>
            ))}
            {hasMoreThanPreview(parsed, previewLines.length) ? (
              <Text color={theme.faint} italic>
                /verbose to see the full diff
              </Text>
            ) : null}
          </Box>
        ) : null}
      </Box>
    );
  }

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
                <Text color={theme.muted}>
                  {narrow ? "      " : "           "}
                </Text>
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
                {narrow
                  ? formatNarrowGutter(line.newLn ?? line.oldLn)
                  : formatGutter(line.oldLn, line.newLn)}
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

/**
 * Find the first run of +/- lines and return up to `maxLines` of it.
 * Used by the non-verbose preview so the user can see *something* of
 * the change without expanding the full diff.
 */
function firstChangePreview(
  parsed: ParsedLine[],
  maxLines: number,
): ParsedLine[] {
  const start = parsed.findIndex((l) => l.kind === "add" || l.kind === "del");
  if (start === -1) return [];
  const out: ParsedLine[] = [];
  for (let i = start; i < parsed.length && out.length < maxLines; i++) {
    const l = parsed[i];
    if (l.kind === "add" || l.kind === "del") {
      out.push(l);
    } else if (l.kind === "ctx") {
      // include immediately-adjacent context for readability
      if (out.length > 0) out.push(l);
    } else {
      // hunk/meta — stop the preview; the next change is far away
      break;
    }
  }
  return out;
}

function hasMoreThanPreview(parsed: ParsedLine[], shown: number): boolean {
  const totalChanges = parsed.filter(
    (l) => l.kind === "add" || l.kind === "del",
  ).length;
  return totalChanges > shown;
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
  return `${o}   ${n}`;
}

function formatNarrowGutter(ln?: number): string {
  return ln != null ? String(ln).padStart(4, " ") : "    ";
}
