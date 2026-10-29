import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawn, type ChildProcess } from 'node:child_process';
import {
  TuiMainScreen, ProcessTerminal, Container, Editor, Markdown, Text,
  matchesKey, Key, SelectList, Input, type Component,
} from '@coderai/pi-tui';
import { editorTheme, markdownTheme } from './theme.js';
import { sanitizeTerminalText, TerminalSanitizer } from './sanitize.js';
import { CompletionProvider } from './completion.js';
import type { ApprovalRequest } from '@coderai/sdk';
import { ApprovalDialog } from './approval-dialog.js';
import { TrustDialog } from './trust-dialog.js';

export class TerminalView {
  readonly tui: TuiMainScreen;
  readonly editor: Editor;
  readonly transcript = new Container();
  readonly status = new Text('Starting…', 1, 0);
  readonly queuePane = new Text('', 1, 0);
  private assistant?: Markdown;
  private assistantText = '';
  private lastAssistantText = '';
  private thinking?: Text;
  private thinkingText = '';
  private thinkingSanitizer = new TerminalSanitizer();
  private assistantSanitizer = new TerminalSanitizer();
  private dialog?: { resolve: (answer: string) => void };
  private dialogs = Promise.resolve('');
  private stopped = false;
  private externalEditor?: ChildProcess;
  private shellOutput?: Text;
  private shellTail = '';
  private shellSanitizer = new TerminalSanitizer();
  private details: { component: Text; full: string; label: string }[] = [];
  private detailsExpanded = false;
  private editorCommand?: string;
  private pastePending = false;
  private pasteInput: string[] = [];
  onSubmit?: (text: string) => void;
  onInterrupt?: () => void;
  onEscape?: () => boolean;
  onExit?: () => void;
  onPlanToggle?: () => void;
  onExternalEditor?: () => void;
  onPasteMedia?: () => Promise<boolean>;
  onSteer?: () => void;
  onRecallQueue?: () => boolean;
  constructor(home: string, cwd: string, private commands: { name: string; description?: string }[]) {
    this.tui = new TuiMainScreen(new ProcessTerminal(), true, join(home, 'logs'));
    this.editor = new Editor(this.tui, editorTheme);
    this.setWorkspace(cwd);
    this.tui.addChild({
      render: width => width >= 55 ? [
        ' ██████╗ ██████╗ ██████╗ ███████╗██████╗  █████╗ ██╗',
        '██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗██╔══██╗██║',
        '██║     ██║   ██║██║  ██║█████╗  ██████╔╝███████║██║',
        '██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗██╔══██║██║',
        '╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║██║  ██║██║',
        ' ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝',
      ] : ['CoderAI'],
      invalidate: () => {},
    });
    this.tui.addChild(new Text('CoderAI · /help · Enter send · Shift+Tab plan · Ctrl+S steer · Ctrl+C cancel/quit', 1, 1));
    this.tui.addChild(this.transcript); this.tui.addChild(this.queuePane); this.tui.addChild(this.status); this.tui.addChild(this.editor);
    this.tui.setFocus(this.editor);
    this.editor.onSubmit = text => {
      if (this.dialog) {
        this.dismissDialog(text); return;
      }
      if (text.trim()) { this.editor.addToHistory(text); this.onSubmit?.(text); }
    };
    this.tui.addInputListener(data => {
      if (matchesKey(data, Key.ctrl('c'))) {
        this.dismissDialog();
        this.onInterrupt?.(); return { consume: true };
      }
      if (this.pastePending) { this.pasteInput.push(data); return { consume: true }; }
      if (!this.dialog && this.onPasteMedia && matchesKey(data, process.platform === 'win32' ? 'alt+v' : Key.ctrl('v'))) {
        const paste = this.onPasteMedia;
        this.pastePending = true;
        void paste().then(handled => { if (!handled && !this.stopped && !this.dialog) this.editor.handleInput(data); }).catch(error => this.message(`Clipboard paste failed: ${String(error)}`)).finally(async () => {
          while (this.pasteInput.length && !this.stopped && !this.dialog) {
            const item = this.pasteInput.shift()!;
            // Keep an immediately queued Enter from being treated as part of a paste burst.
            if (matchesKey(item, Key.enter)) await new Promise(resolve => setTimeout(resolve, 250));
            if (!this.stopped && !this.dialog) this.editor.handleInput(item);
          }
          this.pasteInput.length = 0;
          this.pastePending = false;
        });
        return { consume: true };
      }
      if (matchesKey(data, Key.ctrl('d')) && !this.editor.getText()) { this.onExit?.(); return { consume: true }; }
      if (matchesKey(data, Key.escape) && this.dialog) {
        this.dismissDialog(); return { consume: true };
      }
      if (matchesKey(data, Key.escape) && this.onEscape?.()) return { consume: true };
      if (!this.dialog && matchesKey(data, Key.ctrl('s'))) {
        this.onSteer?.(); return { consume: true };
      }
      if (!this.dialog && !this.editor.getText() && matchesKey(data, Key.up) && this.onRecallQueue?.()) return { consume: true };
      if (matchesKey(data, Key.shift('tab')) && !this.dialog) {
        this.onPlanToggle?.(); return { consume: true };
      }
      if (matchesKey(data, Key.ctrl('g')) && !this.dialog) {
        this.onExternalEditor?.(); return { consume: true };
      }
      if (matchesKey(data, Key.ctrl('o')) && !this.dialog) {
        this.detailsExpanded = !this.detailsExpanded;
        for (const detail of this.details) this.renderDetail(detail);
        this.tui.requestRender(); return { consume: true };
      }
      return undefined;
    });
  }
  start(): void { this.tui.start(); }
  get dialogOpen(): boolean { return this.dialog !== undefined; }
  setWorkspace(cwd: string, additionalDirs: readonly string[] = []): void { this.editor.setAutocompleteProvider(new CompletionProvider(this.commands, cwd, additionalDirs)); }
  setCommands(commands: { name: string; description?: string }[], cwd: string, additionalDirs: readonly string[] = []): void { this.commands = commands; this.setWorkspace(cwd, additionalDirs); }
  stop(): void {
    this.stopped = true;
    this.externalEditor?.kill();
    this.dismissDialog();
    this.tui.stop();
  }
  dismissDialog(answer = ''): void {
    const pending = this.dialog;
    if (!pending) return;
    this.dialog = undefined;
    this.editor.setText('');
    pending.resolve(answer);
  }
  message(text: string): void {
    this.transcript.addChild(new Text(sanitizeTerminalText(text), 1, 0)); this.tui.requestRender();
  }
  detail(label: string, text: string): void {
    const detail = { component: new Text('', 1, 0), label: sanitizeTerminalText(label), full: sanitizeTerminalText(text) };
    this.details.push(detail); this.renderDetail(detail); this.transcript.addChild(detail.component); this.tui.requestRender();
  }
  private renderDetail(detail: { component: Text; full: string; label: string }): void {
    const lines = detail.full.split('\n');
    const clipped = lines.length > 8 || detail.full.length > 2000;
    const content = this.detailsExpanded || !clipped ? detail.full : lines.slice(0, 8).join('\n').slice(0, 2000) + '\n… Ctrl+O to expand';
    detail.component.setText(`${detail.label}${content ? `\n${content}` : ''}`);
  }
  beginShell(): void { this.shellTail = ''; this.shellSanitizer = new TerminalSanitizer(); this.shellOutput = new Text('', 1, 0); this.transcript.addChild(this.shellOutput); }
  shellDelta(text: string): void {
    this.shellTail = (this.shellTail + this.shellSanitizer.write(text)).slice(-16384);
    this.shellOutput?.setText(this.shellTail.split('\n').slice(-10).join('\n'));
    this.tui.requestRender();
  }
  endShell(): void { if (this.shellOutput) this.transcript.removeChild(this.shellOutput); this.shellOutput = undefined; }
  clearTranscript(): void {
    this.transcript.clear(); this.details = []; this.lastAssistantText = ''; this.beginAssistant(); this.tui.requestRender();
  }
  markdown(text: string): void {
    this.transcript.addChild(new Markdown(sanitizeTerminalText(text), 1, 0, markdownTheme)); this.tui.requestRender();
  }
  beginAssistant(newTurn = true): void { this.assistant = undefined; this.assistantText = ''; if (newTurn) this.lastAssistantText = ''; this.assistantSanitizer = new TerminalSanitizer(); this.thinking = undefined; this.thinkingText = ''; this.thinkingSanitizer = new TerminalSanitizer(); }
  delta(text: string): void {
    this.thinking = undefined; this.thinkingText = ''; this.thinkingSanitizer = new TerminalSanitizer();
    if (!this.assistant) { this.assistant = new Markdown('', 1, 0, markdownTheme); this.transcript.addChild(this.assistant); }
    const clean = this.assistantSanitizer.write(text); this.assistantText += clean; this.lastAssistantText += clean; this.assistant.setText(this.assistantText); this.tui.requestRender();
  }
  setLastAssistantText(text: string): void { this.lastAssistantText = sanitizeTerminalText(text); }
  copyLastAssistant(): boolean {
    if (!this.lastAssistantText) return false;
    process.stdout.write(`\x1b]52;c;${Buffer.from(this.lastAssistantText).toString('base64')}\x07`);
    return true;
  }
  setEditorCommand(command: string): void { this.editorCommand = command; }
  async editExternally(): Promise<void> {
    if (this.externalEditor) return;
    const command = this.editorCommand || process.env['VISUAL'] || process.env['EDITOR'];
    if (!command?.trim()) { this.message('Set VISUAL or EDITOR, or use /editor <command>.'); return; }
    const dir = await mkdtemp(join(tmpdir(), 'coderai-edit-'));
    const file = join(dir, 'prompt.md');
    let suspended = false;
    try {
      await writeFile(file, this.editor.getText(), { mode: 0o600 });
      if (this.stopped) return;
      this.tui.stop(); suspended = true;
      await new Promise<void>(resolve => setImmediate(resolve));
      if (this.stopped) return;
      const code = await new Promise<number | null>((resolve, reject) => {
        const quoted = process.platform === 'win32' ? `"${file.replaceAll('"', '\\"')}"` : `'${file.replaceAll("'", "'\\''")}'`;
        const child = spawn(`${command} ${quoted}`, { shell: true, stdio: 'inherit' });
        this.externalEditor = child;
        child.once('error', reject); child.once('exit', resolve);
      });
      if (code === 0 && !this.stopped) this.editor.setText((await readFile(file, 'utf8')).replaceAll('\r\n', '\n').replace(/\n$/, ''));
      else if (!this.stopped) this.message('External editor did not complete successfully.');
    } finally {
      this.externalEditor = undefined;
      await rm(dir, { recursive: true, force: true }).catch(() => {});
      if (!this.stopped && suspended) { process.stdin.pause(); this.tui.start(); this.tui.setFocus(this.editor); this.tui.requestRender(true); }
    }
  }
  thinkingDelta(text: string): void {
    if (!this.thinking) { this.thinking = new Text('', 1, 0); this.transcript.addChild(this.thinking); }
    this.thinkingText += this.thinkingSanitizer.write(text);
    this.thinking.setText(`Thinking: ${this.thinkingText}`); this.tui.requestRender();
  }
  setQueue(items: readonly { text: string; attachments?: string[] }[]): void {
    const lines = items.slice(0, 3).map((item, index) => `  ${index + 1}. ${item.text.replace(/\s+/g, ' ').slice(0, 100)}${item.attachments?.length ? ` (${item.attachments.length} attachments)` : ''}`);
    this.queuePane.setText(items.length ? sanitizeTerminalText(`Queued input · /queue to manage\n${lines.join('\n')}${items.length > 3 ? `\n  … ${items.length - 3} more` : ''}`) : '');
    this.tui.requestRender();
  }
  setStatus(text: string): void { this.status.setText(sanitizeTerminalText(text)); this.tui.requestRender(); }
  async ask(text: string, status = 'Waiting for your answer · Enter submit · Esc cancel'): Promise<string> {
    const answer = this.dialogs.then(() => {
      if (this.stopped) return '';
      const draft = this.editor.getText();
      this.editor.setText('');
      this.message(`─────────────────────────────────────────\n${text}\nEnter submit · Esc cancel\n─────────────────────────────────────────`);
      this.setStatus(status);
      return new Promise<string>(resolve => {
        this.dialog = { resolve: value => {
          if (!this.stopped) this.editor.setText(draft);
          resolve(value);
        } };
      });
    });
    this.dialogs = answer;
    return answer;
  }
  async trustWorkspace(workDir: string, exitOnCancel: boolean, disclosure?: string): Promise<boolean> {
    const answer = await this.modal(
      finish => new TrustDialog(workDir, exitOnCancel, () => this.tui.terminal.rows, finish, () => this.tui.requestRender(), disclosure),
      `Waiting for workspace trust · choose Trust and continue or ${exitOnCancel ? 'exit' : 'cancel'}`,
    );
    return answer === 'yes';
  }
  async approve(request: ApprovalRequest): Promise<string> {
    return this.modal(
      finish => new ApprovalDialog(request, () => this.tui.terminal.rows, finish, () => this.tui.requestRender()),
      'Waiting for approval · choose an action in the approval window',
    );
  }
  private async modal(create: (finish: (answer: string) => void) => Component, status: string): Promise<string> {
    const answer = this.dialogs.then(() => {
      if (this.stopped) return '';
      const draft = this.editor.getText();
      this.editor.setText('');
      this.setStatus(status);
      return new Promise<string>(resolve => {
        const card = create(value => this.dismissDialog(value));
        const overlay = this.tui.showOverlay(card, { width: 88, anchor: 'center', margin: 1 });
        this.dialog = { resolve: value => {
          overlay.hide();
          if (!this.stopped) { this.tui.setFocus(this.editor); this.editor.setText(draft); }
          this.tui.requestRender();
          resolve(value);
        } };
      });
    });
    this.dialogs = answer;
    return answer;
  }
  async choose(title: string, items: { value: string; label: string; description?: string }[]): Promise<string> {
    const answer = this.dialogs.then(() => {
      if (this.stopped || !items.length) return '';
      const draft = this.editor.getText();
      this.message(title);
      const list = new SelectList(items, 8, editorTheme.selectList);
      this.transcript.addChild(list);
      this.tui.setFocus(list);
      this.setStatus('↑/↓ choose · Enter select · Esc cancel');
      this.tui.requestRender();
      return new Promise<string>(resolve => {
        this.dialog = { resolve: value => {
          this.transcript.removeChild(list);
          if (!this.stopped) { this.tui.setFocus(this.editor); this.editor.setText(draft); }
          this.tui.requestRender();
          resolve(value);
        } };
        list.onSelect = item => this.dismissDialog(item.value);
        list.onCancel = () => this.dismissDialog();
        list.onSelectionChange = () => this.tui.requestRender();
      });
    });
    this.dialogs = answer;
    return answer;
  }
  async chooseSearchable(title: string, items: { value: string; label: string; description?: string }[]): Promise<string> {
    const answer = this.dialogs.then(() => {
      if (this.stopped || !items.length) return '';
      const draft = this.editor.getText();
      this.message(title);
      const input = new Input({ prompt: 'Search: ', placeholder: 'Type to filter' });
      let list: SelectList;
      const update = () => {
        const query = input.getValue().toLowerCase();
        list = new SelectList(items.filter(item => `${item.label} ${item.description ?? ''} ${item.value}`.toLowerCase().includes(query)), 8, editorTheme.selectList);
        list.onSelect = item => this.dismissDialog(item.value);
        list.onCancel = () => this.dismissDialog();
        list.onSelectionChange = () => this.tui.requestRender();
      };
      update();
      const component: Component = {
        render: width => [...input.render(width), ...list.render(width)],
        invalidate: () => { input.invalidate(); list.invalidate(); },
        handleInput: data => {
          if (matchesKey(data, Key.up) || matchesKey(data, Key.down) || matchesKey(data, Key.enter) || matchesKey(data, Key.escape)) list.handleInput(data);
          else { input.handleInput(data); update(); }
          this.tui.requestRender();
        },
      };
      this.transcript.addChild(component); this.tui.setFocus(component);
      this.setStatus('Type to search · ↑/↓ choose · Enter select · Esc cancel'); this.tui.requestRender();
      return new Promise<string>(resolve => {
        this.dialog = { resolve: value => {
          this.transcript.removeChild(component);
          if (!this.stopped) { this.tui.setFocus(this.editor); this.editor.setText(draft); }
          this.tui.requestRender(); resolve(value);
        } };
      });
    });
    this.dialogs = answer;
    return answer;
  }
  async chooseMultiple(title: string, items: readonly { label: string; description?: string }[], otherLabel = 'Other'): Promise<string[] | null> {
    const selected = new Set<number>();
    let cursor = 0;
    const answer = this.dialogs.then(() => {
      if (this.stopped) return '';
      const draft = this.editor.getText();
      this.message(title);
      let list: SelectList;
      const createList = () => {
        list = new SelectList([
          ...items.map((item, index) => ({ value: String(index), label: `${selected.has(index) ? '[x]' : '[ ]'} ${item.label}`, description: item.description })),
          { value: String(items.length), label: `${selected.has(items.length) ? '[x]' : '[ ]'} ${otherLabel}`, description: 'Enter a custom answer after selecting Done' },
          { value: 'done', label: 'Done', description: 'Submit selected answers' },
        ], 8, editorTheme.selectList);
        list.setSelectedIndex(cursor);
        list.onSelectionChange = item => { cursor = item.value === 'done' ? items.length + 1 : Number(item.value); this.tui.requestRender(); };
        list.onSelect = item => {
          if (item.value === 'done') {
            if (!selected.size) { this.setStatus('Select at least one answer, or press Esc to cancel.'); return; }
            this.dismissDialog('done'); return;
          }
          cursor = Number(item.value);
          if (selected.has(cursor)) selected.delete(cursor); else selected.add(cursor);
          createList(); this.tui.requestRender();
        };
        list.onCancel = () => this.dismissDialog();
      };
      createList();
      const component: Component = { render: width => list.render(width), invalidate: () => list.invalidate(), handleInput: data => list.handleInput(data) };
      this.transcript.addChild(component); this.tui.setFocus(component);
      this.setStatus('↑/↓ choose · Enter toggle or submit · Esc cancel'); this.tui.requestRender();
      return new Promise<string>(resolve => {
        this.dialog = { resolve: value => {
          this.transcript.removeChild(component);
          if (!this.stopped) { this.tui.setFocus(this.editor); this.editor.setText(draft); }
          this.tui.requestRender(); resolve(value);
        } };
      });
    });
    this.dialogs = answer;
    if (await answer !== 'done') return null;
    const result = [...selected].filter(index => index < items.length).sort((a, b) => a - b).map(index => items[index]!.label);
    if (selected.has(items.length)) {
      const other = (await this.ask('Enter your custom answer:', 'Enter answer · Esc dismiss')).trim();
      if (other) result.push(other);
    }
    return result;
  }
}
