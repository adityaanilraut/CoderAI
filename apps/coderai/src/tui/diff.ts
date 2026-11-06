import chalk from 'chalk';
import { colors } from './theme.js';
import { sanitizeTerminalText } from './sanitize.js';

type Change = { kind: 'context' | 'add' | 'delete'; text: string };

// Myers' shortest edit path; cap work and trace memory for very large replacements.
function changes(before: string[], after: string[]): Change[] | undefined {
  if (!before.length) return after.map(text => ({ kind: 'add', text }));
  if (!after.length) return before.map(text => ({ kind: 'delete', text }));
  const limit = Math.min(before.length + after.length, 1000);
  const offset = limit + 1;
  const frontier = new Int32Array(2 * limit + 3);
  const trace: Int32Array[] = [];
  let work = 0;
  for (let depth = 0; depth <= limit; depth++) {
    trace.push(frontier.slice());
    for (let diagonal = -depth; diagonal <= depth; diagonal += 2) {
      if (++work > 2_000_000) return undefined;
      const index = offset + diagonal;
      let x = diagonal === -depth || (diagonal !== depth && frontier[index - 1]! < frontier[index + 1]!)
        ? frontier[index + 1]! : frontier[index - 1]! + 1;
      let y = x - diagonal;
      while (x < before.length && y < after.length && before[x] === after[y]) {
        x++; y++;
        if (++work > 2_000_000) return undefined;
      }
      frontier[index] = x;
      if (x < before.length || y < after.length) continue;
      const result: Change[] = [];
      for (let step = depth; step >= 0; step--) {
        const previous = trace[step]!;
        const k = x - y;
        const previousK = k === -step || (k !== step && previous[offset + k - 1]! < previous[offset + k + 1]!) ? k + 1 : k - 1;
        const previousX = previous[offset + previousK]!;
        const previousY = previousX - previousK;
        while (x > previousX && y > previousY) {
          result.push({ kind: 'context', text: before[--x]! }); y--;
        }
        if (step === 0) break;
        if (x === previousX) result.push({ kind: 'add', text: after[--y]! });
        else result.push({ kind: 'delete', text: before[--x]! });
      }
      return result.reverse();
    }
  }
  return undefined;
}

export function previewDiff(before: string, after: string): string { return formatDiff(before, after, false); }
export function unifiedDiff(before: string, after: string): string { return formatDiff(before, after, true); }

function formatDiff(before: string, after: string, unified: boolean): string {
  if (before === after) return '(No changes)';
  const split = (text: string) => text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
  const edits = changes(split(before), split(after));
  if (!edits) return unified ? 'Diff omitted: too many changes to compare interactively.' : 'Diff preview omitted: too many changes to compare interactively. Review the full file before approving.';
  let oldLine = 1, newLine = 1;
  const numbered = edits.map(edit => {
    const line = { ...edit, oldLine, newLine };
    if (edit.kind !== 'add') oldLine++;
    if (edit.kind !== 'delete') newLine++;
    return line;
  });
  const ranges: { start: number; end: number }[] = [];
  for (let index = 0; index < edits.length; index++) {
    if (edits[index]!.kind === 'context') continue;
    const start = Math.max(0, index - 3), end = Math.min(edits.length, index + 4);
    const last = ranges.at(-1);
    if (last && start <= last.end) last.end = Math.max(last.end, end);
    else ranges.push({ start, end });
  }
  const digits = String(Math.max(oldLine, newLine)).length;
  return ranges.flatMap(({ start, end }) => {
    const hunk = numbered.slice(start, end);
    const oldCount = hunk.filter(line => line.kind !== 'add').length;
    const newCount = hunk.filter(line => line.kind !== 'delete').length;
    const first = hunk[0]!;
    return [
      `@@ -${first.oldLine - (oldCount ? 0 : 1)},${oldCount} +${first.newLine - (newCount ? 0 : 1)},${newCount} @@`,
      ...hunk.flatMap(line => {
        const old = line.kind === 'add' ? '' : String(line.oldLine);
        const next = line.kind === 'delete' ? '' : String(line.newLine);
        const sign = line.kind === 'add' ? '+' : line.kind === 'delete' ? '-' : ' ';
        return [
          unified ? `${sign}${line.text.replace(/\n$/, '')}` : `${old.padStart(digits)} ${next.padStart(digits)} │ ${sign} ${line.text.replace(/\n$/, '')}`,
          ...(!line.text.endsWith('\n') ? ['\\ No newline at end of file'] : []),
        ];
      }),
    ];
  }).join('\n');
}

// Sanitize untrusted content before applying our own ANSI styles.
export function styleDiffPreview(text: string): string {
  return sanitizeTerminalText(text).split('\n').map(line => {
    if (/^\s*\d*\s+\d*\s+│ \+ /.test(line)) return chalk.hex(colors.success)(line);
    if (/^\s*\d*\s+\d*\s+│ - /.test(line)) return chalk.hex(colors.error)(line);
    if (line.startsWith('@@ ')) return chalk.hex(colors.primary)(line);
    return line;
  }).join('\n');
}
