import React, {useEffect, useMemo, useRef, useState} from "react";
import {Box, Text, useInput} from "ink";
import {theme} from "../theme.js";
import {HELP_MENU_ENTRIES} from "../helpMenu.js";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as crypto from "node:crypto";

export interface PromptProps {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /**
   * When true, Ctrl+C has been tapped once and is armed to exit on the
   * next tap. We surface that as a transient line below the input so the
   * user knows what the next keystroke will do.
   */
  exitHint?: boolean;
  /**
   * Working directory used to scope per-project history. Falls back to
   * process.cwd() when omitted.
   */
  cwd?: string;
}

const MAX_HISTORY = 200;

/**
 * Persist prompt history per project AND globally.
 *
 * Each cwd gets its own history file (hashed) so up-arrow surfaces prompts
 * the user typed in *this* project first. A small global file aggregates
 * recent prompts across projects so the picker still shows useful history
 * in a fresh repo. Reads from project-first, then global as a fallback.
 */
function historyDir(): string {
  return path.join(os.homedir(), ".coderAI", "history");
}

function projectHistoryPath(cwd: string): string {
  const h = crypto.createHash("sha1").update(cwd).digest("hex").slice(0, 12);
  return path.join(historyDir(), `prompt-${h}.json`);
}

function globalHistoryPath(): string {
  return path.join(os.homedir(), ".coderAI", "prompt_history.json");
}

function readHistoryFile(p: string): string[] {
  try {
    const raw = fs.readFileSync(p, "utf8");
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed.filter((x) => typeof x === "string");
  } catch {
    // missing or corrupt — start fresh
  }
  return [];
}

function loadHistory(cwd: string): string[] {
  const project = readHistoryFile(projectHistoryPath(cwd));
  const global = readHistoryFile(globalHistoryPath());
  // De-dupe while preserving order: project entries first (most recent for
  // this repo), then any global entries not already in the project list.
  const seen = new Set<string>(project);
  for (const item of global) {
    if (!seen.has(item)) {
      project.push(item);
      seen.add(item);
    }
  }
  return project.slice(-MAX_HISTORY);
}

function saveHistory(cwd: string, items: string[]) {
  const slice = items.slice(-MAX_HISTORY);
  const writes: Array<[string, string]> = [
    [projectHistoryPath(cwd), JSON.stringify(slice) + "\n"],
    [globalHistoryPath(), JSON.stringify(slice) + "\n"],
  ];
  try {
    fs.mkdirSync(historyDir(), {recursive: true});
    for (const [p, body] of writes) fs.writeFileSync(p, body, "utf8");
  } catch {
    // best-effort
  }
}

const SLASH_NAMES = HELP_MENU_ENTRIES.map((e) => e.slash);

/**
 * Multi-line prompt with slash-command tab completion, Ctrl+R history
 * search, and project-scoped history.
 *
 *   ❯ help me find the bug in
 *     coderAI/agent.py — it crashes on startup
 *     /commands  ^R history  esc/^C cancel  ^C^C quit
 *
 * Newline strategies (terminals vary in what they can transmit):
 *   - `\` at end of line + Enter   → bash-style continuation
 *   - Shift+Enter                  → modern terminals (kitty, iTerm2 cfg)
 *   - Esc then Enter               → fallback for everyone else
 *   - Multi-line paste (bracketed) → kept intact
 */
export function Prompt({onSubmit, disabled, placeholder, exitHint, cwd}: PromptProps) {
  const projectCwd = cwd ?? process.cwd();
  const [value, setValue] = useState("");
  const [cursor, setCursor] = useState(0);

  const history = useRef<string[]>(loadHistory(projectCwd));
  const histPos = useRef<number>(history.current.length);
  const draft = useRef<string>("");

  // Reverse-i-search overlay state. When `query` is non-null the prompt is
  // in search mode: the input row shows the active match and a query line
  // appears underneath.
  const [searchQuery, setSearchQuery] = useState<string | null>(null);
  const [searchHitIdx, setSearchHitIdx] = useState<number>(-1);

  // Esc-then-Enter newline fallback: when the user presses Esc, set this
  // flag for one keystroke; if the next key is Enter, insert a newline.
  const escArmed = useRef(false);
  useEffect(() => {
    if (!escArmed.current) return;
    const t = setTimeout(() => {
      escArmed.current = false;
    }, 800);
    return () => clearTimeout(t);
  }, [value]);

  const submit = (text: string) => {
    if (disabled) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    const hist = history.current;
    if (hist[hist.length - 1] !== trimmed) {
      hist.push(trimmed);
      if (hist.length > MAX_HISTORY) hist.shift();
      saveHistory(projectCwd, hist);
    }
    histPos.current = hist.length;
    draft.current = "";
    setValue("");
    setCursor(0);
    setSearchQuery(null);
    onSubmit(trimmed);
  };

  // Tab-complete the active /command. If exactly one match remains, fill
  // it in; otherwise advance to the longest common prefix.
  const tabComplete = () => {
    if (!value.startsWith("/")) return false;
    const head = value.split(/\s/, 1)[0]; // "/he"
    const rest = value.slice(head.length);
    const matches = SLASH_NAMES.filter((n) => n.startsWith(head));
    if (matches.length === 0) return false;
    if (matches.length === 1) {
      const next = matches[0] + (rest.length === 0 ? " " : rest);
      setValue(next);
      setCursor(matches[0].length + (rest.length === 0 ? 1 : Math.min(cursor, next.length)));
      return true;
    }
    const lcp = longestCommonPrefix(matches);
    if (lcp.length > head.length) {
      const next = lcp + rest;
      setValue(next);
      setCursor(lcp.length);
      return true;
    }
    return false;
  };

  const insertAtCursor = (s: string) => {
    setValue((v) => v.slice(0, cursor) + s + v.slice(cursor));
    setCursor((c) => c + s.length);
  };

  const moveCursor = (delta: number) => {
    setCursor((c) => Math.max(0, Math.min(value.length, c + delta)));
  };

  // Locate cursor's (row, col) within the current value for vertical motion.
  const cursorRC = useMemo(() => rowColAt(value, cursor), [value, cursor]);
  const lines = useMemo(() => value.split("\n"), [value]);

  const moveLine = (delta: number) => {
    const targetRow = cursorRC.row + delta;
    if (targetRow < 0) {
      // Step out of multi-line into history (only if first row).
      if (lines.length === 1 && history.current.length > 0) historyStep(-1);
      return;
    }
    if (targetRow >= lines.length) {
      if (lines.length === 1) historyStep(1);
      return;
    }
    const col = Math.min(cursorRC.col, lines[targetRow].length);
    setCursor(offsetAtRowCol(value, targetRow, col));
  };

  const historyStep = (delta: number) => {
    const hist = history.current;
    if (hist.length === 0) return;
    if (histPos.current === hist.length) draft.current = value;
    const next = Math.max(0, Math.min(hist.length, histPos.current + delta));
    histPos.current = next;
    const text = next === hist.length ? draft.current : (hist[next] ?? "");
    setValue(text);
    setCursor(text.length);
  };

  // Reverse search: walk backwards from `from` for the most recent hit.
  const findMatch = (q: string, from: number): number => {
    if (!q) return -1;
    const hist = history.current;
    for (let i = Math.min(from, hist.length - 1); i >= 0; i--) {
      if (hist[i].toLowerCase().includes(q.toLowerCase())) return i;
    }
    return -1;
  };

  useInput(
    (input, key) => {
      if (disabled) return;

      // -------------------- Reverse-i-search mode --------------------
      if (searchQuery !== null) {
        if (key.escape) {
          setSearchQuery(null);
          return;
        }
        if (key.return) {
          // Accept the current hit into the buffer; user hits Enter again to send.
          if (searchHitIdx >= 0) {
            const text = history.current[searchHitIdx];
            setValue(text);
            setCursor(text.length);
          }
          setSearchQuery(null);
          return;
        }
        if (key.ctrl && input === "r") {
          // Step to an older match.
          const next = findMatch(searchQuery, searchHitIdx - 1);
          if (next >= 0) setSearchHitIdx(next);
          return;
        }
        if (key.backspace || key.delete) {
          const q = searchQuery.slice(0, -1);
          setSearchQuery(q);
          setSearchHitIdx(findMatch(q, history.current.length - 1));
          return;
        }
        if (input.length >= 1 && !key.ctrl && !key.meta && !key.tab) {
          const q = searchQuery + input;
          setSearchQuery(q);
          setSearchHitIdx(findMatch(q, history.current.length - 1));
        }
        return;
      }

      // -------------------- Edit mode --------------------

      // Ctrl+R: enter reverse-i-search.
      if (key.ctrl && input === "r") {
        // Only enter search mode when there's history to search — otherwise
        // it's a no-op and the user gets no useful feedback.
        if (history.current.length > 0) {
          setSearchQuery("");
          setSearchHitIdx(history.current.length - 1);
        }
        return;
      }

      // Esc arms newline-on-next-Enter.
      if (key.escape) {
        escArmed.current = true;
        return;
      }

      // Tab: complete slash command.
      if (key.tab) {
        tabComplete();
        return;
      }

      if (key.return) {
        // Newline cases (in priority order):
        //   1. Esc was just pressed → newline (works on any terminal)
        //   2. Shift+Enter sent a distinguishable code → newline
        //   3. line ends with `\` → strip the backslash and newline
        const lineEndsWithBackslash =
          cursor > 0 && value[cursor - 1] === "\\";
        if (escArmed.current) {
          escArmed.current = false;
          insertAtCursor("\n");
          return;
        }
        if (key.shift) {
          insertAtCursor("\n");
          return;
        }
        if (lineEndsWithBackslash) {
          setValue((v) => v.slice(0, cursor - 1) + "\n" + v.slice(cursor));
          // cursor moved: removed `\`, added `\n` at same offset → net 0.
          return;
        }
        submit(value);
        return;
      }

      if (key.upArrow) {
        moveLine(-1);
        return;
      }
      if (key.downArrow) {
        moveLine(1);
        return;
      }
      if (key.leftArrow) {
        moveCursor(-1);
        return;
      }
      if (key.rightArrow) {
        moveCursor(1);
        return;
      }
      if (key.backspace || key.delete) {
        if (cursor === 0) return;
        setValue((v) => v.slice(0, cursor - 1) + v.slice(cursor));
        setCursor((c) => Math.max(0, c - 1));
        return;
      }
      // Ctrl+A / Ctrl+E: BOL/EOL within current row.
      if (key.ctrl && input === "a") {
        setCursor((c) => offsetAtRowCol(value, rowColAt(value, c).row, 0));
        return;
      }
      if (key.ctrl && input === "e") {
        setCursor((c) => {
          const {row} = rowColAt(value, c);
          return offsetAtRowCol(value, row, lines[row].length);
        });
        return;
      }
      // Alt/Option+Left / Alt+Right: word-level cursor movement.
      if ((key.leftArrow && (key.ctrl || key.meta)) || (key.meta && input === "b")) {
        setCursor(wordBoundaryLeft(value, cursor));
        return;
      }
      if ((key.rightArrow && (key.ctrl || key.meta)) || (key.meta && input === "f")) {
        setCursor(wordBoundaryRight(value, cursor));
        return;
      }
      // Ctrl+U: clear to BOL on current row.
      if (key.ctrl && input === "u") {
        const {row, col} = cursorRC;
        const start = offsetAtRowCol(value, row, 0);
        setValue((v) => v.slice(0, start) + v.slice(start + col));
        setCursor(start);
        return;
      }
      // Ctrl+K: clear to EOL on current row.
      if (key.ctrl && input === "k") {
        const {row, col} = cursorRC;
        const start = offsetAtRowCol(value, row, col);
        const end = offsetAtRowCol(value, row, lines[row].length);
        setValue((v) => v.slice(0, start) + v.slice(end));
        return;
      }

      // Plain text — covers single chars AND multi-char paste. Ink hands
      // bracketed-paste content as a single `input` string, so multi-line
      // pastes lose nothing as long as we accept the whole buffer.
      if (input.length >= 1 && !key.ctrl && !key.meta) {
        // Reset history pointer once the user starts editing fresh content.
        histPos.current = history.current.length;
        draft.current = value + input;
        insertAtCursor(input);
      }
    },
    {isActive: !disabled},
  );

  // Render the buffer with an inverse-video cursor. For an empty buffer we
  // show a single space so the cursor block is visible. The placeholder is
  // shown in a separate dim line when the buffer is empty.
  const renderedLines = useMemo(() => {
    return renderWithCursor(lines, cursorRC, !disabled);
  }, [lines, cursorRC, disabled]);

  const showPlaceholder = !value && !searchQuery;
  const placeholderText =
    placeholder ?? (disabled ? "waiting for agent…" : "ask anything, or type / for commands");

  // Search overlay rendering
  if (searchQuery !== null) {
    const hit =
      searchHitIdx >= 0 ? history.current[searchHitIdx] : "";
    return (
      <Box flexDirection="column" paddingX={theme.spacing.sm}>
        <Box>
          <Text color={theme.warning} bold>
            (reverse-i-search)`<Text color={theme.text}>{searchQuery}</Text>`{" "}
          </Text>
          <Text color={hit ? theme.text : theme.faint}>
            {hit || "no match"}
          </Text>
        </Box>
        <Box marginTop={theme.spacing.sm}>
          <Text color={theme.faint}>
            <Text color={theme.muted}>^R</Text> older
            {theme.glyph.separator}
            <Text color={theme.muted}>↵</Text> accept
            {theme.glyph.separator}
            <Text color={theme.muted}>esc</Text> cancel
          </Text>
        </Box>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" paddingX={theme.spacing.sm}>
      <Box flexDirection="row">
        <Text color={disabled ? theme.faint : theme.accent} bold>
          {theme.glyph.caret}{" "}
        </Text>
        <Box flexDirection="column">
          {showPlaceholder ? (
            <Text color={theme.faint}>
              {!disabled ? <Text inverse> </Text> : null}
              {placeholderText}
            </Text>
          ) : (
            renderedLines.map((node, i) => <Box key={i}>{node}</Box>)
          )}
        </Box>
      </Box>
      {exitHint ? (
        <Box marginTop={theme.spacing.sm}>
          <Text color={theme.warning}>
            {theme.glyph.warn} ^C again to exit
          </Text>
        </Box>
      ) : !disabled ? (
        <Box marginTop={theme.spacing.sm}>
          <Text color={theme.faint}>
            <Text color={theme.muted}>↵</Text> send
            {theme.glyph.separator}
            <Text color={theme.muted}>\\↵</Text> newline
            {theme.glyph.separator}
            <Text color={theme.muted}>↹</Text> complete
            {theme.glyph.separator}
            <Text color={theme.muted}>^R</Text> history
            {theme.glyph.separator}
            <Text color={theme.muted}>esc/^C</Text> cancel
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}

function rowColAt(s: string, offset: number): {row: number; col: number} {
  let row = 0;
  let col = 0;
  for (let i = 0; i < offset && i < s.length; i++) {
    if (s[i] === "\n") {
      row++;
      col = 0;
    } else {
      col++;
    }
  }
  return {row, col};
}

function offsetAtRowCol(s: string, row: number, col: number): number {
  let off = 0;
  let r = 0;
  for (; r < row && off < s.length; off++) {
    if (s[off] === "\n") r++;
  }
  // off now points at start of target row
  const lineEnd = s.indexOf("\n", off);
  const end = lineEnd === -1 ? s.length : lineEnd;
  return Math.min(off + col, end);
}

function longestCommonPrefix(strs: string[]): string {
  if (strs.length === 0) return "";
  let prefix = strs[0];
  for (let i = 1; i < strs.length; i++) {
    while (!strs[i].startsWith(prefix) && prefix.length > 0) {
      prefix = prefix.slice(0, -1);
    }
    if (!prefix) break;
  }
  return prefix;
}

function renderWithCursor(
  lines: string[],
  cursorRC: {row: number; col: number},
  showCursor: boolean,
): React.ReactNode[] {
  return lines.map((ln, i) => {
    const isCursorRow = i === cursorRC.row && showCursor;
    if (!isCursorRow) {
      return (
        <Text key={i} color={theme.text}>
          {ln.length === 0 ? " " : ln}
        </Text>
      );
    }
    const before = ln.slice(0, cursorRC.col);
    const at = ln[cursorRC.col] ?? " ";
    const after = ln.slice(cursorRC.col + 1);
    return (
      <Text key={i} color={theme.text}>
        {before}
        <Text inverse>{at}</Text>
        {after}
      </Text>
    );
  });
}

function wordBoundaryLeft(value: string, cursor: number): number {
  let i = cursor;
  while (i > 0 && value[i - 1] === " ") i--;
  while (i > 0 && value[i - 1] !== " ") i--;
  return Math.max(0, i);
}

function wordBoundaryRight(value: string, cursor: number): number {
  let i = cursor;
  while (i < value.length && value[i] !== " ") i++;
  while (i < value.length && value[i] === " ") i++;
  return Math.min(value.length, i);
}
