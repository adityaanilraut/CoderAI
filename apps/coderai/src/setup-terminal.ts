import { createInterface, emitKeypressEvents } from 'node:readline';
import type { Key } from 'node:readline';
import { PassThrough, Writable } from 'node:stream';
import chalk from 'chalk';
import { fuzzyFilter, parseOsc11BackgroundColor, StdinBuffer, truncateToWidth, type RgbColor } from '@coderai/pi-tui';
import { sanitizeTerminalText } from './tui/sanitize.js';
import { colors, refreshTerminalTheme } from './tui/theme.js';

export type SetupOption = { label: string; description?: string };
export type Prompt = ((question: string, secret?: boolean) => Promise<string>) & {
  select?: (title: string, items: SetupOption[], initial: number, back: string) => Promise<number>;
  dispose?: () => void;
};
export class SetupCancelled extends Error {}
export const setupAbort = () => Object.assign(new Error('Setup cancelled.'), { name: 'AbortError' });
export const singleLine = (text: string) => sanitizeTerminalText(text).replace(/[\n\t]/g, ' ');

export function setupMenuLines(state: {
  title: string; items: (SetupOption & { index: number })[]; selected: number;
  query: string; shortcut: string; columns: number; rows: number;
}): string[] {
  const { title, items, selected, query, shortcut } = state;
  const count = Math.max(1, Math.min(9, state.rows - 7));
  const start = Math.max(0, Math.min(selected - Math.floor(count / 2), items.length - count));
  const width = Math.max(1, state.columns - 1);
  const clip = (text: string) => truncateToWidth(text, width, '…');
  const muted = (text: string) => chalk.hex(colors.textMuted)(text);
  const backLabel = items.find(item => item.index === -1)?.label;
  const backHint = backLabel && backLabel !== 'Back' ? singleLine(backLabel) : 'back';
  const content = [
    chalk.hex(colors.primary).bold(`  ${singleLine(title)}`),
    muted(query ? `  Search: ${singleLine(query)}` : shortcut ? `  Option: ${shortcut} · Enter select` : '  ↑/↓ move · Enter select · type to search'),
    muted(`  Esc ${backHint} · Ctrl+C exit`),
    ...items.slice(start, start + count).map((item, index) => {
      const label = `${item.index === -1 ? '0' : item.index + 1}. ${singleLine(item.label)}`;
      return start + index === selected ? chalk.hex(colors.primary)(`  ❯ ${label}`) : `    ${label}`;
    }),
  ];
  if (query && items.length === 1 && items[0]?.index === -1) content.push(muted('  No matching options · Backspace to edit'));
  const row = items[selected];
  if (row?.description) content.push(muted(`    ${singleLine(row.description)}`));
  if (items.length > count) content.push(muted(`  ${selected + 1} / ${items.length} options`));
  return content.map(clip).slice(0, Math.max(1, state.rows - 2));
}

/** Owns terminal input only for the duration of each question; every exit restores it. */
export function terminalPrompt(home?: string): Prompt {
  const source = process.stdin, stream = new PassThrough();
  const input = stream as unknown as NodeJS.ReadStream;
  const wasRaw = source.isRaw;
  const resume = stream.resume.bind(stream), pause = stream.pause.bind(stream);
  Object.defineProperties(input, { isTTY: { get: () => source.isTTY }, isRaw: { get: () => source.isRaw } });
  input.setRawMode = mode => { source.setRawMode(mode); return input; };
  input.resume = () => { source.resume(); resume(); return input; };
  input.pause = () => { source.pause(); pause(); return input; };
  const buffer = new StdinBuffer({ oscTimeout: 1000 });
  let backgroundReply: ((color: RgbColor | undefined) => void) | undefined;
  let queried = false;
  let disposed = false;
  buffer.on('data', data => {
    // Replies must never reach readline, even after timeout or while entering a secret.
    if (data.startsWith('\x1b]11;')) { backgroundReply?.(parseOsc11BackgroundColor(data)); return; }
    stream.write(data);
  });
  buffer.on('paste', content => stream.write(`\x1b[200~${content}\x1b[201~`));
  const onData = (data: Buffer | string) => buffer.process(data);
  const onEnd = () => stream.end();
  source.setEncoding('utf8'); source.on('data', onData); source.once('end', onEnd);
  emitKeypressEvents(input);
  const prompt: Prompt = (question, secret = false) => new Promise((resolve, reject) => {
    let finished = false;
    const output = new Writable({ write(chunk, _encoding, callback) {
      if (!secret) process.stdout.write(chunk);
      callback();
    } });
    const readline = createInterface({ input, output, terminal: true, historySize: 0 });
    const finish = (error?: Error, answer = '') => {
      if (finished) return;
      finished = true;
      input.off('keypress', onKey);
      process.off('SIGINT', onInterrupt);
      process.off('SIGTERM', onInterrupt);
      readline.close();
      if (secret || error) process.stdout.write('\n');
      error ? reject(error) : resolve(answer);
    };
    const onKey = (_text: string, key: Key) => {
      if (key.name === 'escape') finish(new SetupCancelled());
    };
    const onInterrupt = () => finish(setupAbort());
    input.on('keypress', onKey);
    process.on('SIGINT', onInterrupt);
    process.on('SIGTERM', onInterrupt);
    readline.on('SIGINT', onInterrupt);
    readline.once('close', () => finish(setupAbort()));
    if (secret) process.stdout.write(question);
    readline.question(secret ? '' : question, answer => finish(undefined, answer));
  });

  prompt.select = (title, items, initial, back) => new Promise((resolve, reject) => {
    const output = process.stdout;
    const wasRaw = input.isRaw;
    const choices = [...items.map((item, index) => ({ ...item, index })), { label: back, index: -1 }];
    let selected = Math.max(0, Math.min(initial, choices.length - 1));
    let query = '';
    let shortcut = '';
    let lines = 0;
    let finished = false;
    const filtered = () => [...fuzzyFilter(choices.filter(item => item.index !== -1), query, item => `${item.label} ${item.description ?? ''}`), ...choices.filter(item => item.index === -1)];
    const paint = () => {
      const rows = filtered();
      selected = Math.min(selected, rows.length - 1);
      const content = setupMenuLines({ title, items: rows, selected, query, shortcut, columns: output.columns || 80, rows: output.rows || 24 });
      if (lines) output.write(`\x1b[${lines}A`);
      output.write('\r\x1b[J' + content.join('\n') + '\n');
      lines = content.length;
    };
    const finish = (value: number, error?: Error) => {
      if (finished) return;
      finished = true;
      input.off('keypress', onKey);
      input.off('end', onEnd);
      output.off('resize', paint);
      process.off('SIGINT', onInterrupt);
      process.off('SIGTERM', onInterrupt);
      input.setRawMode(wasRaw);
      input.pause();
      if (lines) output.write(`\x1b[${lines}A\r\x1b[J`);
      output.write('\x1b[?25h');
      if (!error) output.write(`${chalk.hex(colors.success)('  ✓')} ${singleLine(title)} ${chalk.hex(colors.textMuted)('›')} ${singleLine(value ? items[value - 1]!.label : back)}\n`);
      error ? reject(error) : resolve(value);
    };
    const onInterrupt = () => finish(0, setupAbort());
    const onEnd = () => finish(0, setupAbort());
    const onKey = (text: string, key: Key) => {
      if (finished) return;
      if (key.ctrl && (key.name === 'c' || key.name === 'd')) { onInterrupt(); return; }
      if (key.name === 'escape') { finish(0); return; }
      const rows = filtered();
      if (key.name === 'return') {
        if (shortcut) { const value = Number(shortcut); if (value <= items.length) finish(value); else output.write('\x07'); }
        else { const row = rows[selected]; if (row) finish(row.index === -1 ? 0 : row.index + 1); }
        return;
      }
      if (key.name === 'up') selected = (selected + rows.length - 1) % rows.length;
      else if (key.name === 'down' || key.name === 'tab') selected = (selected + 1) % rows.length;
      else if (key.name === 'home') selected = 0;
      else if (key.name === 'end') selected = rows.length - 1;
      else if (key.name === 'backspace') {
        if (shortcut) shortcut = shortcut.slice(0, -1);
        else query = [...query].slice(0, -1).join('');
        selected = 0; paint(); return;
      } else if (!query && /^[0-9]$/.test(text ?? '')) {
        shortcut = (shortcut + text).slice(0, 6);
        if (Number(shortcut) <= items.length) selected = Number(shortcut) === 0 ? rows.length - 1 : Number(shortcut) - 1;
        paint(); return;
      } else if (text && !key.ctrl && !key.meta && !/[\x00-\x1f\x7f]/.test(text)) { query += shortcut + text; shortcut = ''; selected = 0; }
      else return;
      shortcut = '';
      paint();
    };
    input.on('keypress', onKey);
    input.once('end', onEnd);
    output.on('resize', paint);
    process.on('SIGINT', onInterrupt);
    process.on('SIGTERM', onInterrupt);
    input.setRawMode(true);
    input.resume();
    output.write('\n\x1b[?25l');
    paint();
    if (home && !queried) {
      queried = true;
      void refreshTerminalTheme({
        queryTerminalBackgroundColor: () => new Promise(resolve => { backgroundReply = resolve; output.write('\x1b]11;?\x07'); }),
        invalidate() {}, requestRender: () => { if (!finished) paint(); },
      }, home, () => !disposed).finally(() => { backgroundReply = undefined; });
    }
  });
  prompt.dispose = () => {
    disposed = true;
    source.off('data', onData); source.off('end', onEnd); source.pause();
    buffer.destroy(); stream.destroy(); source.setRawMode(!!wasRaw);
  };
  return prompt;
}
