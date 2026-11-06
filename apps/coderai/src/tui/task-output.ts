import { Key, matchesKey, wrapTextWithAnsi, type Component } from '@coderai/pi-tui';
import type { Session } from '@coderai/sdk';
import { dialogBodyHeight, dialogFrame } from './dialogs.js';
import { sanitizeTerminalText } from './sanitize.js';
import { singleLine } from './status.js';

export const TASK_OUTPUT_LIMIT = 1024 * 1024;

export class TaskOutputViewer implements Component {
  private output = '';
  private latest = '';
  private status = 'Loading…';
  private error = '';
  private following = true;
  private scroll = 0;
  private maxScroll = 0;
  private page = 1;
  private lines: string[] = [];
  private width = 0;
  private renderedOutput?: string;
  constructor(private readonly taskId: string, private readonly rows: () => number,
    private readonly finish: (answer: string) => void, private readonly refresh: () => void) {}

  setOutput(output: string, status: string): void {
    this.latest = sanitizeTerminalText(output).replace(/\t/g, '   ');
    // Freeze the reading snapshot while paused, including when the SDK ring buffer rotates.
    if (this.following) this.output = this.latest;
    this.status = singleLine(status); this.error = ''; this.refresh();
  }
  setError(error: unknown): void { this.error = singleLine(String(error)); this.refresh(); }
  invalidate(): void { this.width = 0; }
  handleInput(data: string): void {
    if (matchesKey(data, Key.escape) || matchesKey(data, Key.enter) || matchesKey(data, 'q') || matchesKey(data, Key.shift('q'))) { this.finish(''); return; }
    if (matchesKey(data, Key.end) || matchesKey(data, 'f') || matchesKey(data, Key.shift('f'))) {
      this.following = true; this.output = this.latest;
    } else if (matchesKey(data, Key.home)) { this.following = false; this.scroll = 0; }
    else if (matchesKey(data, Key.up) || matchesKey(data, Key.pageUp)) {
      this.following = false;
      this.scroll = Math.max(0, this.scroll - (matchesKey(data, Key.up) ? 1 : this.page));
    } else if (matchesKey(data, Key.down) || matchesKey(data, Key.pageDown)) {
      this.scroll = Math.min(this.maxScroll, this.scroll + (matchesKey(data, Key.down) ? 1 : this.page));
      if (this.scroll === this.maxScroll) { this.following = true; this.output = this.latest; }
    }
    this.refresh();
  }
  render(width: number): string[] {
    const inner = Math.max(1, width - 4);
    if (inner !== this.width || this.renderedOutput !== this.output) {
      this.width = inner; this.renderedOutput = this.output;
      this.lines = wrapTextWithAnsi(this.output || '(No output)', inner);
    }
    this.page = Math.max(1, dialogBodyHeight(this.rows()) - 1);
    this.maxScroll = Math.max(0, this.lines.length - this.page);
    this.scroll = this.following ? this.maxScroll : Math.min(this.scroll, this.maxScroll);
    const position = `${this.scroll + 1}–${Math.min(this.lines.length, this.scroll + this.page)}/${this.lines.length}`;
    const mode = this.following ? 'Following' : `Paused${this.latest !== this.output ? ' · new output' : ''}`;
    return dialogFrame(`Task output · ${this.taskId}`, [
      this.error ? `Refresh failed: ${this.error}` : `${this.status} · captured ≤1 MiB chars`,
      ...this.lines.slice(this.scroll, this.scroll + this.page),
    ], ['↑/↓ · PgUp/PgDn · Home · End/F follow', `${mode} · ${position} · Esc close`], width, this.rows());
  }
}

/** Poll through the SDK only while the dialog is mounted; ignore late replies after close. */
export function watchTaskOutput(session: Pick<Session, 'getBackgroundTaskOutput' | 'listBackgroundTasks'>,
  taskId: string, viewer: TaskOutputViewer, interval = 750): () => void {
  let disposed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    try {
      const [output, tasks] = await Promise.all([
        session.getBackgroundTaskOutput(taskId, { tail: TASK_OUTPUT_LIMIT }), session.listBackgroundTasks(),
      ]);
      if (!disposed) viewer.setOutput(output, tasks.find(task => task.taskId === taskId)?.status ?? 'Task unavailable');
    } catch (error) { if (!disposed) viewer.setError(error); }
    finally { if (!disposed) { timer = setTimeout(() => void poll(), interval); timer.unref(); } }
  };
  void poll();
  return () => { disposed = true; if (timer) clearTimeout(timer); };
}
