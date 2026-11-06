import { Key, matchesKey, type Component } from '@coderai/pi-tui';
import type { SessionSummary } from '@coderai/sdk';
import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { ChoiceDialog } from './dialogs.js';

export function relativeSessionTime(timestamp: number, now = Date.now()): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return 'Unknown time';
  const minutes = Math.max(0, Math.floor((now - timestamp) / 60_000));
  if (!minutes) return 'Just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  return `${Math.floor(minutes / 1440)}d ago`;
}

type BrowserState = { all: boolean; selected?: string; notice?: string };
export class SessionPicker implements Component {
  private readonly picker: ChoiceDialog;
  constructor(private readonly sessions: readonly SessionSummary[], private readonly current: { id: string; workDir: string },
    private readonly state: BrowserState, rows: () => number, private readonly finish: (answer: string) => void, private readonly refresh: () => void) {
    this.picker = new ChoiceDialog('Choose a conversation:\nCtrl+A workspace/all · Ctrl+R rename · Ctrl+X delete', [], rows,
      id => this.complete('resume', id), refresh, { searchable: true, initial: state.selected });
    this.update();
  }
  private update(): void {
    this.picker.setNotice(`${this.state.notice ? `${this.state.notice}\n` : ''}${this.state.all ? 'All workspaces' : 'Current workspace'} · Ctrl+A switch\nCtrl+R rename · Ctrl+X delete`);
    this.picker.setItems([...this.sessions].filter(session => this.state.all || session.workDir === this.current.workDir)
      .sort((a, b) => b.updatedAt - a.updatedAt || b.createdAt - a.createdAt)
      .map(session => ({ value: session.id,
        label: `${session.id === this.current.id ? '[current] ' : ''}${session.title || session.lastPrompt || '(untitled)'} · ${relativeSessionTime(session.updatedAt)}`,
        description: `${session.workDir} · ${session.id}${session.lastPrompt ? `\n${session.lastPrompt}` : ''}`,
      })));
  }
  private complete(action: string, id = this.picker.selection?.value): void {
    if (!id) { this.finish(''); return; }
    this.state.selected = id; this.finish(JSON.stringify({ action, id }));
  }
  invalidate(): void { this.picker.invalidate(); }
  get focused(): boolean { return this.picker.focused; }
  set focused(value: boolean) { this.picker.focused = value; }
  handleInput(data: string): void {
    if (matchesKey(data, Key.ctrl('a'))) { this.state.all = !this.state.all; this.update(); this.refresh(); }
    else if (matchesKey(data, Key.ctrl('r'))) this.complete('rename');
    else if (matchesKey(data, Key.ctrl('x'))) this.complete('delete');
    else this.picker.handleInput(data);
  }
  render(width: number): string[] {
    return this.picker.render(width);
  }
}

export async function browseSessions(runtime: Runtime, view: TerminalView): Promise<string> {
  const state: BrowserState = { all: false };
  for (;;) {
    const current = runtime.session!;
    const sessions = await runtime.harness.listSessions();
    const result = await view.showDialog(finish => new SessionPicker(sessions, current, state, () => view.tui.terminal.rows, finish, () => view.tui.requestRender()),
      'Search conversations · Ctrl+A workspace/all · Ctrl+R rename · Ctrl+X delete');
    if (!result) return '';
    const { action, id } = JSON.parse(result) as { action: string; id: string };
    const selected = sessions.find(session => session.id === id);
    if (!selected) continue;
    if (action === 'resume') return id;
    if (action === 'rename') {
      const title = (await view.ask('Rename conversation\nEnter a title for this saved conversation.', 'Enter save · Esc cancel', selected.title ?? '')).trim();
      if (title) { await runtime.harness.renameSession({ id, title }); state.notice = `Title: ${title}`; }
    } else if (action === 'delete') {
      if (id === current.id) { state.notice = 'Open conversation cannot be deleted.'; view.notice('This conversation is open. Resume another conversation before deleting it.'); continue; }
      const choice = await view.choose(`Delete conversation?\n${selected.title || '(untitled)'}\n${selected.workDir}\nSaved messages will be permanently removed.`, [
        { value: 'keep', label: 'Keep conversation' }, { value: 'delete', label: 'Delete conversation', description: 'Permanently remove this saved conversation' },
      ]);
      if (choice === 'delete') { await runtime.harness.deleteSession(id); state.notice = 'Conversation deleted.'; state.selected = undefined; }
    }
  }
}
