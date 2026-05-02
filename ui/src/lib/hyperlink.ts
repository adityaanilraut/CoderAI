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
 * Match `path/to/file.ts:42` or `path/to/file.ts:42:7` (line:col), with the
 * file portion permissive enough for typical project paths but bounded so
 * inline code blocks aren't accidentally turned into links.
 *
 *   - alphanumerics, `_`, `-`, `.`, `/` in the path
 *   - must contain at least one `/` OR an extension to avoid matching plain words
 *   - colon-separated 1- to 6-digit line number (col optional)
 */
const FILE_LINE_RE =
  /(\b(?:[\w.\-]+\/)+[\w.\-]+|\b[\w-]+\.[\w-]+)(:\d{1,6})(?::\d{1,4})?/g;

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
    const path = match[1];
    const abs = cwd && !path.startsWith("/") ? `${cwd}/${path}` : path;
    const href = `file://${abs}`;
    segments.push({text: full, href});
    lastIndex = start + full.length;
  }
  if (lastIndex < input.length) {
    segments.push({text: input.slice(lastIndex)});
  }
  return segments;
}
