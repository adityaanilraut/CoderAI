import chalk from 'chalk';
import { truncateToWidth, type Component } from '@coderai/pi-tui';
import type { SessionTodoItem } from '@coderai/sdk';
import { colors } from './theme.js';
import { singleLine } from './status.js';

export function selectVisibleTodos(items: readonly SessionTodoItem[]): readonly SessionTodoItem[] {
  if (items.length <= 3) return items;
  const active = items.map((item, index) => item.status === 'in_progress' ? index : -1).filter(index => index >= 0);
  const picked = new Set(active.slice(0, 2));
  const lastDone = items.findLastIndex(item => item.status === 'done');
  if (picked.size < 2 && lastDone >= 0) picked.add(lastDone);
  for (let index = 0; index < items.length && picked.size < 2; index++) {
    if (items[index]!.status === 'pending') picked.add(index);
  }
  for (let index = items.length - 1; index >= 0 && picked.size < 2; index--) picked.add(index);
  return items.filter((_, index) => picked.has(index));
}

export function todoListFromResult(output: unknown): readonly SessionTodoItem[] | undefined {
  if (typeof output !== 'string') return undefined;
  if (output === 'Todo list cleared.' || output === 'Todo list is empty.') return [];
  const lines = output.replaceAll('\r\n', '\n').split('\n');
  const start = lines.indexOf('Current todo list:');
  if (start < 0) return undefined;
  const items: SessionTodoItem[] = [];
  for (const line of lines.slice(start + 1)) {
    if (!line) break;
    const match = /^ {2}\[(pending|in_progress|done)\] (.+)$/.exec(line);
    if (!match) return undefined;
    items.push({ status: match[1] as SessionTodoItem['status'], title: match[2]! });
  }
  return items;
}

export class TodoListComponent implements Component {
  private items: readonly SessionTodoItem[] = [];
  expanded = false;

  constructor(private readonly pinned = false) {}
  setItems(items: readonly SessionTodoItem[]): void { this.items = items.map(item => ({ ...item, title: singleLine(item.title) })); }
  invalidate(): void {}

  render(width: number): string[] {
    if (width < 3) return [];
    if (!this.items.length) return this.pinned ? [] : [' ' + truncateToWidth('No todos.', Math.max(0, width - 2), '…')];
    const done = this.items.filter(item => item.status === 'done').length;
    const active = this.items.filter(item => item.status === 'in_progress').length;
    // The pinned list is for current work; completed snapshots remain in the transcript.
    if (this.pinned && done === this.items.length) return [];
    const title = `Todos · ${done}/${this.items.length} done${active ? ` · ${active} active` : ''}${this.pinned ? ' · /todos' : ''}`;
    const lines = [chalk.hex(done === this.items.length ? colors.success : colors.textMuted)(title)];
    const visible = this.pinned
      ? [...this.items.filter(item => item.status === 'in_progress'), ...this.items.filter(item => item.status === 'pending')].slice(0, 2)
      : this.expanded ? this.items : selectVisibleTodos(this.items);
    for (const item of visible) {
      if (item.status === 'done') lines.push(chalk.hex(colors.success)('✓ ') + chalk.hex(colors.textMuted).strikethrough(item.title));
      else if (item.status === 'in_progress') lines.push(chalk.hex(colors.primary).bold(`● ${item.title}`));
      else lines.push(chalk.hex(colors.textMuted)('○ ') + item.title);
    }
    const hidden = this.items.length - visible.length;
    if (hidden && !this.pinned) {
      const counts = { done: 0, in_progress: 0, pending: 0 };
      for (const item of this.items) if (!visible.includes(item)) counts[item.status]++;
      const summary = [counts.done ? `${counts.done} done` : '', counts.in_progress ? `${counts.in_progress} active` : '', counts.pending ? `${counts.pending} pending` : ''].filter(Boolean).join(' · ');
      lines.push(chalk.hex(colors.textMuted)(`… +${hidden} more (${summary}) · Ctrl+O expand`));
    }
    return lines.map(line => ' ' + truncateToWidth(line, Math.max(0, width - 2), '…'));
  }
}
