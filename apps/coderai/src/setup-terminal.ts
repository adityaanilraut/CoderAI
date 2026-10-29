import { createInterface, emitKeypressEvents } from 'node:readline';
import type { Key } from 'node:readline';
import { Writable } from 'node:stream';
import chalk from 'chalk';
import { truncateToWidth } from '@coderai/pi-tui';
import { sanitizeTerminalText } from './tui/sanitize.js';

export type SetupOption = { label: string; description?: string };
export type Prompt = ((question: string, secret?: boolean) => Promise<string>) & {
  select?: (title: string, items: SetupOption[], initial: number, back: string) => Promise<number>;
};
export class SetupCancelled extends Error {}
export const setupAbort = () => Object.assign(new Error('Setup cancelled.'), { name: 'AbortError' });
export const singleLine = (text: string) => sanitizeTerminalText(text).replace(/[\n\t]/g, ' ');

/** Owns terminal input only for the duration of each question; every exit restores it. */
export function terminalPrompt(): Prompt {
  emitKeypressEvents(process.stdin);
  const prompt: Prompt = (question, secret = false) => new Promise((resolve, reject) => {
    let finished = false;
    const output = new Writable({ write(chunk, _encoding, callback) {
      if (!secret) process.stdout.write(chunk);
      callback();
    } });
    const readline = createInterface({ input: process.stdin, output, terminal: true, historySize: 0 });
    const finish = (error?: Error, answer = '') => {
      if (finished) return;
      finished = true;
      process.stdin.off('keypress', onKey);
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
    process.stdin.on('keypress', onKey);
    process.on('SIGINT', onInterrupt);
    process.on('SIGTERM', onInterrupt);
    readline.on('SIGINT', onInterrupt);
    readline.once('close', () => finish(setupAbort()));
    if (secret) process.stdout.write(question);
    readline.question(secret ? '' : question, answer => finish(undefined, answer));
  });

  prompt.select = (title, items, initial, back) => new Promise((resolve, reject) => {
    const input = process.stdin;
    const output = process.stdout;
    const wasRaw = input.isRaw;
    const choices = [...items, { label: back }];
    let selected = Math.max(0, Math.min(initial, choices.length - 1));
    let query = '';
    let shortcut = '';
    let lines = 0;
    let finished = false;
    const filtered = () => choices.map((item, index) => ({ ...item, index }))
      .filter(item => item.index === items.length || `${item.label} ${item.description ?? ''}`.toLowerCase().includes(query.toLowerCase()));
    const paint = () => {
      const rows = filtered();
      selected = Math.min(selected, rows.length - 1);
      const count = Math.max(1, Math.min(9, (output.rows || 24) - 6));
      const start = Math.max(0, Math.min(selected - Math.floor(count / 2), rows.length - count));
      const width = Math.max(1, (output.columns || 80) - 1);
      const clip = (text: string) => truncateToWidth(text, width);
      const content = [
        clip(chalk.bold.cyan(`  ${singleLine(title)}`)),
        clip(chalk.dim(query ? `  Search: ${singleLine(query)}` : shortcut ? `  Option: ${shortcut} · Enter select · Backspace edit` : '  ↑/↓ move · Enter select · type to search · Esc back · Ctrl+C exit')),
        ...rows.slice(start, start + count).map((item, index) => {
          const label = `${item.index === items.length ? '0' : item.index + 1}. ${singleLine(item.label)}`;
          return clip(start + index === selected ? chalk.cyan(`  ❯ ${label}`) : `    ${label}`);
        }),
      ];
      if (query && rows.length === 1) content.push(clip(chalk.dim('  No matching options · Backspace to edit your search')));
      const row = rows[selected];
      if (row?.description) content.push(clip(chalk.dim(`    ${singleLine(row.description)}`)));
      if (rows.length > count) content.push(clip(chalk.dim(`  ${selected + 1} / ${rows.length} options`)));
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
      if (!error) output.write(`${chalk.dim('  ✓')} ${singleLine(title)} ${chalk.dim('›')} ${singleLine(value ? items[value - 1]!.label : back)}\n`);
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
        else { const row = rows[selected]; if (row) finish(row.index === items.length ? 0 : row.index + 1); }
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
  });
  return prompt;
}
