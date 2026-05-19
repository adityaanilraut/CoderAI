/**
 * OSC-8 hyperlink renderer.
 *
 * Modern terminals (iTerm2, kitty, WezTerm, Alacritty 0.13+, GNOME Terminal,
 * Windows Terminal) support OSC 8 escape sequences that turn plain text into
 * a clickable link. Terminals that don't support OSC 8 simply render the
 * inner text without the hyperlink chrome — there is no fallback to worry
 * about, the escape bytes are stripped automatically.
 *
 * Usage:
 *   <Text>{hyperlink("file:///abs/path", "src/foo.ts:42")}</Text>
 *
 * Note: Ink prints the text as-is (including ANSI escapes), so embedding the
 * sequence in a Text element is safe — Ink does not interpret these.
 */

export const ESC = "\u001b";
export const BEL = "\u0007";

export function hyperlink(href: string, label: string): string {
  return `${ESC}]8;;${href}${BEL}${label}${ESC}]8;;${BEL}`;
}

/**
 * Match file references in two flavours:
 *
 *   1. `path/to/file.ts[:line[:col]]` — any path that has at least one `/`.
 *      Line/col are optional. The slash is enough signal to be sure this is
 *      a file ref and not a generic word.
 *   2. `file.ts:42[:col]` — bare filename, but only when a line number is
 *      attached. Without `:line`, `foo.bar` is too ambiguous (could be a
 *      method call, an object key, etc.).
 *
 * Match groups: full match (always), so we use `match[0]` for the visible
 * text — line/col are part of the href when present.
 */
const FILE_LINE_RE =
  /(?:\b(?:[\w.\-]+\/)+[\w.\-]+(?::\d{1,6}(?::\d{1,4})?)?|\b[\w-]+\.[\w-]+:\d{1,6}(?::\d{1,4})?)/g;

export interface TextSegment {
  text: string;
  /** When set, the segment is a clickable file:line link. */
  href?: string;
}

/**
 * Tokenize a string into plain segments and file:line link segments. Used by
 * the assistant message renderer so file references are clickable on
 * supporting terminals.
 *
 * Resolves relative paths against `cwd` so the resulting `href` is absolute
 * (file://) — relative URIs aren't reliably honored by terminal emulators.
 */
export function linkifyFileRefs(input: string, cwd?: string): TextSegment[] {
  const segments: TextSegment[] = [];
  let lastIndex = 0;
  for (const match of input.matchAll(FILE_LINE_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      segments.push({text: input.slice(lastIndex, start)});
    }
    const full = match[0];
    // Split `path[:line[:col]]` into the path portion for resolving against
    // cwd. The href keeps the full ref so terminals that support `file://…:N`
    // can jump to the line directly.
    const colonAt = indexOfPathLineSeparator(full);
    const pathOnly = colonAt === -1 ? full : full.slice(0, colonAt);
    const abs =
      cwd && !pathOnly.startsWith("/") ? `${cwd}/${pathOnly}` : pathOnly;
    const lineTail = colonAt === -1 ? "" : full.slice(colonAt);
    const href = `file://${abs}${lineTail}`;
    segments.push({text: full, href});
    lastIndex = start + full.length;
  }
  if (lastIndex < input.length) {
    segments.push({text: input.slice(lastIndex)});
  }
  return segments;
}

/**
 * Find the colon that separates the path from `:line` in a match like
 * `foo/bar.ts:42:7`. Returns -1 when there is no line annotation. We scan
 * for the first `:` followed by a digit so a path containing `:` (rare on
 * Windows-style refs we don't try to match anyway) doesn't trip us up.
 */
function indexOfPathLineSeparator(full: string): number {
  for (let i = 0; i < full.length - 1; i++) {
    if (full[i] === ":" && full[i + 1] >= "0" && full[i + 1] <= "9") return i;
  }
  return -1;
}
