import { QuestionDialog } from './questions.js';
import { openBrowser } from '../browser.js';
import { identity } from '../identity.js';
import { copyTextToClipboard } from '../clipboard-text.js';
import { markdownTerminal } from './markdown-context.js';
import { UpdatesPanel } from './updates.js';
import { StatusLineCommandRunner, type StatusLinePayload } from './status-line-command.js';
import { loadTuiConfig, DEFAULT_TUI_CONFIG, type TuiConfig } from './config.js';
import { setRenderLatex, setMermaidMode } from './markdown.js';
import { setTheme } from './theme.js';
import { Markdown } from './markdown.js';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { spawn, type ChildProcess } from 'node:child_process';
import {
  TuiMainScreen, TuiAltScreen, ScrollView, VStack, getNativeClipboard, ProcessTerminal, Editor, Text,
  matchesKey, Key, Container, type Component,
} from '@coderai/pi-tui';
import { colors, editorTheme, markdownTheme, refreshTerminalTheme } from './theme.js';
import { sanitizeTerminalText, TerminalSanitizer } from './sanitize.js';
import { CompletionProvider } from './completion.js';
import type { ApprovalRequest, ContextMessage, SessionStatus, SessionTodoItem, TurnStepRetryingEvent } from '@coderai/sdk';
import { ApprovalDialog } from './approval-dialog.js';
import { TrustDialog } from './trust-dialog.js';
import chalk from 'chalk';
import { ActivityIndicator, StatusFooter } from './status.js';
import { TodoListComponent } from './todos.js';
import { recoveryNotice } from './recovery.js';
import { ChoiceDialog, DocumentDialog, PromptDialog, type Choice } from './dialogs.js';
import { PromptHistory, mergePromptHistory } from './prompt-history.js';
import { ConversationTranscript, type ToolStart } from './transcript.js';
import { AttachmentPane } from './attachments.js';
import { GitStatusPoller } from './git-status.js';
import { TaskOutputViewer, watchTaskOutput } from './task-output.js';
import type { Session } from '@coderai/sdk';
import { SubagentActivityStore, agentActivityText } from './subagent-activity.js';
import { WelcomePanel } from './welcome.js';

export class TerminalView {
  preferences: TuiConfig = structuredClone(DEFAULT_TUI_CONFIG);
  private statusCommand?: StatusLineCommandRunner;
  private statusPayload?: StatusLinePayload;
  private terminalFocused = true;
  onSessionReloaded?: () => void;
  onPreferencesChanged?: (config: TuiConfig) => void;
  readonly tui: TuiMainScreen | TuiAltScreen;
  readonly editor: Editor;
  readonly transcript = new ConversationTranscript();
  readonly status = new ActivityIndicator(() => this.tui.requestRender());
  readonly footer = new StatusFooter();
  readonly welcome = new WelcomePanel();
  readonly todoPane = new TodoListComponent(true);
  readonly btwPane = new Container();
  readonly updates = new UpdatesPanel(() => this.tui.terminal.rows);
  readonly agents = new SubagentActivityStore();
  readonly queuePane = new Text('', 1, 0);
  readonly attachmentPane = new AttachmentPane(() => this.tui.requestRender(), () => this.tui.terminal.rows);
  private gitBranch: string | null = null;
  private readonly git = new GitStatusPoller(status => { this.gitBranch = status?.branch ?? null; this.footer.setGitStatus(status); if (this.statusPayload) { this.statusPayload = { ...this.statusPayload, gitBranch: this.gitBranch }; this.statusCommand?.maybeRefresh(this.statusPayload); } this.tui.requestRender(); });
  private dialog?: { resolve: (answer: string) => void; onCancel?: (data: string) => void };
  private dialogs = Promise.resolve('');
  private stopped = false;
  private started = false;
  private externalEditor?: ChildProcess;
  private shellOutput?: Text;
  private shellTail = '';
  private shellSanitizer = new TerminalSanitizer();
  private details: { component: Text; full: string; label: string; collapsed: boolean }[] = [];
  private todoCards: TodoListComponent[] = [];
  private detailsExpanded = false;
  private editorCommand?: string;
  private pastePending = false;
  private pasteInput: string[] = [];
  private readonly promptHistory: PromptHistory;
  private historyWorkspace = '';
  private historyVersion = 0;
  private recentHistory: string[] = [];
  suggestFiles?: (workDir: string, query: string) => Promise<import('@coderai/sdk').SuggestFilesResult | undefined>;
  onSubmit?: (text: string) => void;
  onInterrupt?: () => void;
  onEscape?: () => boolean;
  onExit?: () => void;
  onPlanToggle?: () => void;
  onExternalEditor?: () => void;
  onPasteMedia?: () => Promise<boolean>;
  onSteer?: () => void;
  onRecallQueue?: () => boolean;
  onManageAttachments?: () => void;
  onPanelScroll?: (direction: 'up' | 'down') => boolean;
  onTaskBrowser?: () => void;
  constructor(private readonly home: string, cwd: string, private commands: { name: string; description?: string }[], initial: TuiConfig = structuredClone(DEFAULT_TUI_CONFIG)) {
    this.preferences = initial;
    const terminal = new ProcessTerminal({ oscTimeout: 1000 });
    this.tui = initial.tuiMode === 'fullscreen' ? new TuiAltScreen(terminal, true, join(home, 'logs'), {
      openUrl: url => { void openBrowser(url).then(opened => { if (!opened && !this.stopped) this.notice(`Could not open link. Open it manually: ${url}`); }).catch(error => this.error(error)); },
      refocusMouseGuardMs: 200,
      scrollToEndIndicator: () => chalk.hex(colors.primary)(' ↓ Jump to bottom '),
      copySelection: async text => { try { await copyTextToClipboard(text); return true; } catch { return false; } },
      onRightClickPaste: () => {
        const focused = this.tui instanceof TuiAltScreen ? this.tui.getFocusedComponent() : null;
        void getNativeClipboard()?.getText().then(text => {
          if (!text || this.stopped || !(this.tui instanceof TuiAltScreen) || this.tui.getFocusedComponent() !== focused) return;
          focused?.handleInput?.(`\x1b[200~${text}\x1b[201~`); this.tui.requestRender();
        }).catch(() => {});
      },
    }) : new TuiMainScreen(terminal, true, join(home, 'logs'));
    markdownTerminal.fullscreen = this.tui.mode === 'fullscreen';
    markdownTerminal.render = () => { if (!this.stopped) this.tui.requestRender(); };
    this.editor = new Editor(this.tui, editorTheme);
    this.promptHistory = new PromptHistory(home);
    this.setWorkspace(cwd);
    this.tui.addChild(this.welcome);
    this.tui.addChild(this.transcript); this.tui.addChild(this.todoPane); this.tui.addChild(this.queuePane); this.tui.addChild(this.status); this.tui.addChild(this.attachmentPane); this.tui.addChild(this.updates); this.tui.addChild(this.btwPane); this.tui.addChild(this.editor); this.tui.addChild(this.footer);
    if (this.tui instanceof TuiAltScreen) {
      const document = new Container(); document.addChild(this.welcome); document.addChild(this.transcript);
      const scroll = new ScrollView(document, { follow: 'end', primary: true, scrollbar: 'auto' });
      const dock = new VStack();
      for (const component of [this.todoPane, this.queuePane, this.status, this.attachmentPane, this.updates, this.btwPane]) dock.addChild(component, { shrink: 1, minSize: 0 });
      dock.addChild(this.editor, { shrink: 0, minSize: 3 });
      dock.addChild(this.footer, { shrink: 0, minSize: 1 });
      const root = new VStack();
      root.addChild(scroll, { basis: 0, grow: 1, minSize: 1 });
      root.addChild(dock, { basis: 'auto', shrink: 1, minSize: 4 });
      this.tui.setLayoutRoot(root);
    }
    this.tui.setFocus(this.editor);
    this.editor.onSubmit = text => {
      if (this.dialog) {
        this.dismissDialog(text); return;
      }
      if (text.trim()) {
        this.editor.addToHistory(text); this.recentHistory.push(text); this.recentHistory = this.recentHistory.slice(-100);
        if (!text.trimStart().startsWith('/')) void this.promptHistory.append(this.historyWorkspace, text).catch(error => {
          const message = `Prompt history could not be saved: ${String(error)}`;
          if (!this.stopped) this.notice(message); else process.stderr.write(sanitizeTerminalText(message) + '\n');
        });
        this.onSubmit?.(text);
      }
    };
    this.tui.addInputListener(data => {
      if (data === '/' && this.tui instanceof TuiAltScreen && !this.tui.isFollowingOutput && !this.tui.hasOverlay() && !this.dialog && !this.editor.getText()) { this.tui.openTranscriptSearch(); return { consume: true }; }
      if (data === '\x1b[I' || data === '\x1b[O') { this.terminalFocused = data === '\x1b[I'; return { consume: true }; }
      if (this.dialog?.onCancel && (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl('c')))) {
        this.dialog.onCancel(data); return { consume: true };
      }
      if (!this.dialog && !this.editor.getText() && (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) && this.onPanelScroll?.(matchesKey(data, Key.pageUp) ? 'up' : 'down')) return { consume: true };
      if (matchesKey(data, Key.ctrl('c'))) {
        this.dismissDialog();
        this.onInterrupt?.(); return { consume: true };
      }
      if (this.pastePending) { this.pasteInput.push(data); return { consume: true }; }
      if (!this.dialog && matchesKey(data, Key.ctrl('t'))) { this.onManageAttachments?.(); return { consume: true }; }
      if (!this.dialog && matchesKey(data, Key.ctrl('b'))) { this.onTaskBrowser?.(); return { consume: true }; }
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
        for (const card of this.todoCards) card.expanded = this.detailsExpanded;
        this.transcript.setExpanded(this.detailsExpanded);
        this.updateExpandHint();
        this.tui.requestRender(); return { consume: true };
      }
      return undefined;
    });
  }
  start(): void { this.started = true; this.tui.start(); this.tui.terminal.write('\x1b[?1004h'); this.status.start(); void this.refreshTheme(); }
  async refreshTheme(): Promise<void> { await refreshTerminalTheme(this.tui, this.home, () => !this.stopped); }
  get dialogOpen(): boolean { return this.dialog !== undefined; }
  setWorkspace(cwd: string, additionalDirs: readonly string[] = []): void {
    this.welcome.setWorkspace(cwd);
    this.transcript.setWorkspace(cwd); this.editor.setAutocompleteProvider(new CompletionProvider(this.commands, cwd, additionalDirs, (root, query) => this.suggestFiles?.(root, query) ?? Promise.resolve(undefined)));
    if (cwd === this.historyWorkspace) return;
    this.historyWorkspace = cwd; this.recentHistory = []; this.editor.setHistory([]);
    const version = ++this.historyVersion;
    void this.promptHistory.load(cwd).then(entries => {
      if (this.stopped || version !== this.historyVersion) return;
      this.editor.setHistory(mergePromptHistory(entries, this.recentHistory));
    }).catch(error => { if (!this.stopped && version === this.historyVersion) this.notice(`Prompt history could not be loaded: ${String(error)}`); });
  }
  async flushHistory(): Promise<void> { await this.promptHistory.flush(); }
  async suspend<T>(action: () => Promise<T>): Promise<T> {
    this.status.stop(); this.tui.stop();
    await new Promise<void>(resolve => setImmediate(resolve));
    try { return await action(); }
    finally {
      if (!this.stopped) { process.stdin.pause(); this.tui.start(); this.tui.terminal.write('\x1b[?1004h'); this.status.start(); this.tui.setFocus(this.editor); this.tui.requestRender(true); }
    }
  }
  setCommands(commands: { name: string; description?: string }[], cwd: string, additionalDirs: readonly string[] = []): void { this.commands = commands; this.setWorkspace(cwd, additionalDirs); }
  stop(): void {
    this.stopped = true;
    this.status.stop();
    this.statusCommand?.dispose();
    this.git.stop(); this.attachmentPane.dispose();
    this.externalEditor?.kill();
    this.dismissDialog();
    this.tui.terminal.write('\x1b[?1004l'); this.tui.stop();
  }
  dismissDialog(answer = ''): void {
    const pending = this.dialog;
    if (!pending) return;
    this.dialog = undefined;
    this.editor.setText('');
    pending.resolve(answer);
  }
  message(text: string): void {
    this.transcript.breakGroup();
    this.transcript.addChild(new Text(sanitizeTerminalText(text), 1, 0)); this.tui.requestRender();
  }
  detail(label: string, text: string, collapsed = false): void {
    this.transcript.breakGroup();
    const detail = { component: new Text('', 1, 0), label: sanitizeTerminalText(label), full: sanitizeTerminalText(text), collapsed };
    this.details.push(detail); this.renderDetail(detail); this.transcript.addChild(detail.component); this.updateExpandHint(); this.tui.requestRender();
  }
  private updateExpandHint(): void {
    const expandable = this.transcript.hasDetails || this.todoCards.length > 0 || this.details.some(detail => detail.collapsed || detail.full.length > 2000 || detail.full.split('\n').length > 8);
    this.footer.setExpandHint(expandable, this.detailsExpanded);
  }
  private renderDetail(detail: { component: Text; full: string; label: string; collapsed: boolean }): void {
    if (detail.collapsed && !this.detailsExpanded) { detail.component.setText(`${detail.label} · Ctrl+O to show details`); return; }
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
    this.transcript.clear(); if (this.tui instanceof TuiAltScreen) this.tui.scrollToBottom(); this.details = []; this.todoCards = []; this.updateExpandHint(); this.setTodos([]); this.tui.requestRender();
  }
  markdown(text: string): void {
    this.transcript.addChild(new Markdown(sanitizeTerminalText(text), 1, 0, markdownTheme)); this.tui.requestRender();
  }
  beginAssistant(newTurn = true): void { this.transcript.beginAssistant(newTurn); }
  delta(text: string): void { this.transcript.delta(text); this.refreshTranscript(); }
  userMessage(text: string): void { this.transcript.userMessage(text); this.refreshTranscript(); }
  toolStarted(call: ToolStart): void { this.transcript.toolStarted(call); this.refreshTranscript(); }
  hideTool(agentId: string, toolCallId: string): void { this.transcript.hideTool(agentId, toolCallId); this.refreshTranscript(); }
  toolDelta(event: { agentId: string; toolCallId: string; name?: string; argumentsPart?: string }): void { this.transcript.toolDelta(event); this.refreshTranscript(); }
  setSessionTitle(title: string): void { this.footer.setTitle(title); this.tui.requestRender(); }
  toolResult(result: { agentId: string; toolCallId: string; output: unknown; isError?: boolean }): void { if (this.updates.isDisplayed(result.agentId, result.toolCallId)) return; this.transcript.toolResult(result); this.refreshTranscript(); }
  toolProgress(agentId: string, toolCallId: string, text: string, replace = false): void { this.transcript.toolProgress(agentId, toolCallId, text, replace); this.refreshTranscript(); }
  finishTurn(agentId = 'main'): void { this.transcript.finishTurn(agentId); this.refreshTranscript(); }
  replayMessage(message: ContextMessage): void { this.transcript.replayMessage(message); this.refreshTranscript(); }
  private refreshTranscript(): void { this.updateExpandHint(); this.tui.requestRender(); }
  async copyLastAssistant(): Promise<'native' | 'osc52' | null> {
    if (!this.transcript.lastAssistantText) return null;
    return copyTextToClipboard(this.transcript.lastAssistantText);
  }

  async reloadPreferences(): Promise<void> {
    await this.applyPreferences(await loadTuiConfig(this.home, text => this.notice(text)));
  }
  async applyPreferences(config: TuiConfig): Promise<void> {
    if (!await setTheme(config.theme, this.home)) throw new Error(`Theme not found or invalid: ${config.theme}`);
    const statusCommandChanged = config.statusLine.command !== this.preferences.statusLine.command;
    this.preferences = config;
    this.editorCommand = config.editorCommand || undefined;
    this.editor.setDisablePasteBurst(config.disablePasteBurst);
    setRenderLatex(config.renderLatex); setMermaidMode(config.markdown.mermaid);
    this.footer.setPreferences(config.statusLine);
    if (statusCommandChanged || (!this.statusCommand && config.statusLine.command)) {
      this.statusCommand?.dispose();
      this.footer.setCustomLine(undefined);
      this.statusCommand = config.statusLine.command ? new StatusLineCommandRunner(config.statusLine.command, () => {
        if (this.stopped) return;
        this.footer.setCustomLine(this.statusCommand?.current() ?? undefined); this.tui.requestRender();
      }) : undefined;
    }
    if (this.statusPayload) this.statusCommand?.maybeRefresh(this.statusPayload);
    this.onPreferencesChanged?.(config);
    if (config.tuiMode !== this.tui.mode) this.notice(`Terminal mode saved as ${config.tuiMode}. Restart CoderAI to switch modes; this session remains in ${this.tui.mode} mode.`);
    this.tui.invalidate(); if (this.started) await this.refreshTheme(); this.tui.requestRender();
  }
  notifyCompletion(): void {
    const config = this.preferences.notifications;
    if (config.enabled && (config.condition === 'always' || !this.terminalFocused)) this.tui.terminal.write('\x07');
  }
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
      this.status.stop(); this.tui.stop(); suspended = true;
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
      if (!this.stopped && suspended) { process.stdin.pause(); this.tui.start(); this.tui.terminal.write('\x1b[?1004h'); this.status.start(); this.tui.setFocus(this.editor); this.tui.requestRender(true); }
    }
  }
  thinkingDelta(text: string): void {
    this.transcript.thinkingDelta(text); this.refreshTranscript();
  }
  setQueue(items: readonly { text: string; attachments?: string[] }[]): void {
    const lines = items.slice(0, 3).map((item, index) => `  ${index + 1}. ${item.text.replace(/\s+/g, ' ').slice(0, 100)}${item.attachments?.length ? ` (${item.attachments.length} attachments)` : ''}`);
    this.queuePane.setText(items.length ? sanitizeTerminalText(`Queued input · /queue to manage\n${lines.join('\n')}${items.length > 3 ? `\n  … ${items.length - 3} more` : ''}`) : '');
    this.tui.requestRender();
  }
  setStatus(text: string): void { this.status.setText(sanitizeTerminalText(text)); this.tui.requestRender(); }
  setSessionStatus(status: SessionStatus, workspace: string, model: string, attachments = 0, sessionId = ''): void {
    this.welcome.setModel(model, !!status.model);
    this.statusPayload = { model, cwd: this.historyWorkspace, gitBranch: this.gitBranch, permissionMode: status.permission, planMode: !!status.planMode, contextUsage: status.maxContextTokens > 0 ? status.contextTokens / status.maxContextTokens : 0, contextTokens: status.contextTokens, maxContextTokens: status.maxContextTokens, sessionId, version: identity.version };
    this.statusCommand?.maybeRefresh(this.statusPayload);
    this.footer.setState(status, workspace, model, attachments); this.tui.requestRender();
  }
  setWorkspaceStatus(cwd: string, attachments: readonly string[]): void {
    if (this.stopped) return;
    this.git.setWorkspace(cwd);
    void this.attachmentPane.setPaths(attachments, cwd).catch(error => this.notice(`Attachments could not be inspected: ${String(error)}`));
  }
  setActivity(label: string, detail = ''): void { this.status.setActivity(label, detail); this.tui.requestRender(); }
  setRetry(retry: TurnStepRetryingEvent): void { this.status.setRetry(retry); this.tui.requestRender(); }
  setTodos(items: readonly SessionTodoItem[]): void { this.todoPane.setItems(items); this.tui.requestRender(); }
  showTodos(items: readonly SessionTodoItem[], expanded = false): void {
    this.transcript.breakGroup();
    const card = new TodoListComponent(); card.setItems(items); card.expanded = expanded || this.detailsExpanded;
    if (!expanded && items.length > 3) this.todoCards.push(card);
    this.updateExpandHint();
    this.transcript.addChild(card); this.tui.requestRender();
  }
  notice(message: string, recovery = '', level: 'error' | 'warning' = 'warning'): void {
    const clean = sanitizeTerminalText(message), guidance = recovery ? '\n' + sanitizeTerminalText(recovery) : '';
    const body = new Text('', 1, 0); let previous = '';
    this.transcript.addChild({
      render: width => {
        const color = level === 'error' ? colors.error : colors.warning;
        if (previous !== color) { previous = color; body.setText(chalk.hex(color)(clean) + guidance); }
        return body.render(width);
      },
      invalidate: () => body.invalidate(),
    });
    this.tui.requestRender();
  }
  error(error: unknown): void {
    const notice = recoveryNotice(error);
    if (notice.cancelled) { this.message(notice.message); return; }
    this.notice(`Error: ${notice.message}`, notice.recovery, 'error');
    const details = error instanceof Error ? error.stack ?? error.message : JSON.stringify(error, null, 2) ?? String(error);
    this.detail('Error details', details, true);
  }
  async ask(text: string, status = 'Waiting for your answer · Enter submit · Esc cancel', initial = ''): Promise<string> {
    return this.showDialog(finish => new PromptDialog(text, () => this.tui.terminal.rows, finish, () => this.tui.requestRender(), initial), status);
  }
  async showDocument(title: string, content: string): Promise<void> {
    await this.showDialog(finish => new DocumentDialog(title, content, () => this.tui.terminal.rows, finish, () => this.tui.requestRender()), '↑/↓ scroll · PgUp/PgDn page · Enter/Esc close');
  }
  async showTaskOutput(session: Session, taskId: string): Promise<void> {
    const task = (await session.listBackgroundTasks()).find(task => task.taskId === taskId);
    const agentId = task?.kind === 'agent' ? task.agentId : undefined;
    if (agentId && this.agents.get(agentId)) { await this.showAgentActivity(agentId); return; }
    let dispose: (() => void) | undefined;
    try {
      await this.showDialog(finish => {
        const viewer = new TaskOutputViewer(taskId, () => this.tui.terminal.rows, finish, () => this.tui.requestRender());
        dispose = watchTaskOutput(session, taskId, viewer);
        return viewer;
      }, 'Task output · PgUp/PgDn page · End follow · Esc return');
    } finally { dispose?.(); }
  }
  async showAgentActivity(agentId: string): Promise<void> {
    let off: (() => void) | undefined;
    try {
      await this.showDialog(finish => {
        const viewer = new TaskOutputViewer(`Agent ${agentId}`, () => this.tui.terminal.rows, finish, () => this.tui.requestRender());
        const update = () => {
          const record = this.agents.get(agentId);
          viewer.setOutput(record ? agentActivityText(record) : 'No activity retained.', record?.status ?? 'Unavailable');
        };
        off = this.agents.subscribe(update); update(); return viewer;
      }, 'Agent activity · PgUp/PgDn page · End follow · Esc return');
    } finally { off?.(); }
  }
  async trustWorkspace(workDir: string, exitOnCancel: boolean, disclosure?: string): Promise<boolean> {
    const answer = await this.showDialog(
      finish => new TrustDialog(workDir, exitOnCancel, () => this.tui.terminal.rows, finish, () => this.tui.requestRender(), disclosure),
      `Waiting for workspace trust · choose Trust and continue or ${exitOnCancel ? 'exit' : 'cancel'}`,
    );
    return answer === 'yes';
  }
  async approve(request: ApprovalRequest): Promise<string> {
    return this.showDialog(
      finish => new ApprovalDialog(request, () => this.tui.terminal.rows, finish, () => this.tui.requestRender()),
      'Waiting for approval · choose an action in the approval window',
    );
  }
  async showDialog(create: (finish: (answer: string) => void) => Component, status: string, options: { onCancel?: (data: string) => void; onDismiss?: () => void } = {}): Promise<string> {
    const answer = this.dialogs.then(() => {
      if (this.stopped) return '';
      const draft = this.editor.getText();
      this.editor.setText('');
      this.setStatus(status);
      return new Promise<string>(resolve => {
        const card = create(value => this.dismissDialog(value));
        const overlay = this.tui.showOverlay(card, { width: 88, anchor: 'center', margin: 1 });
        this.dialog = { onCancel: options.onCancel, resolve: value => {
          options.onDismiss?.();
          overlay.hide();
          if (!this.stopped) { this.tui.setFocus(this.editor); this.editor.setText(draft); }
          this.tui.requestRender();
          resolve(value);
        } };
      });
    });
    this.dialogs = answer.catch(() => '');
    return answer;
  }
  async choose(title: string, items: Choice[]): Promise<string> {
    if (!items.length) return '';
    return this.showDialog(finish => new ChoiceDialog(title, items, () => this.tui.terminal.rows, finish, () => this.tui.requestRender()), '↑/↓ choose · Enter select · Esc cancel');
  }
  async chooseSearchable(title: string, items: Choice[], initial?: string): Promise<string> {
    if (!items.length) return '';
    return this.showDialog(finish => new ChoiceDialog(title, items, () => this.tui.terminal.rows, finish, () => this.tui.requestRender(), { searchable: true, initial }), 'Type to search · ↑/↓ choose · Enter select · Esc cancel');
  }
  async answerQuestions(request: import('@coderai/sdk').QuestionRequest): Promise<Record<string, string> | null> {
    let panel: QuestionDialog;
    await this.showDialog(finish => {
      panel = new QuestionDialog(request, () => this.tui.terminal.rows, finish, () => this.tui.requestRender());
      return panel;
    }, 'Answer questions · Esc previous · Ctrl+C cancel all', {
      onCancel: data => panel!.handleInput(data),
    });
    return panel!?.answers ?? null;
  }
  async chooseMultiple(title: string, items: readonly { label: string; description?: string }[], otherLabel = 'Other', otherDescription = 'Enter a custom answer after selecting Done'): Promise<string[] | null> {
    let picker: ChoiceDialog;
    const answer = await this.showDialog(finish => {
      picker = new ChoiceDialog(title, [
        ...items.map((item, index) => ({ value: String(index), label: item.label, description: item.description })),
        ...(otherLabel ? [{ value: 'other', label: otherLabel, description: otherDescription }] : []),
        { value: '\0done', label: 'Done', description: 'Submit selected answers' },
      ], () => this.tui.terminal.rows, finish, () => this.tui.requestRender(), { multiple: true });
      return picker;
    }, '↑/↓ choose · Space/Enter toggle · Done submits · Esc cancel');
    if (answer !== '\0done') return null;
    const values = picker!.values;
    const result = values.filter(value => value !== 'other').map(value => items[Number(value)]!.label);
    if (values.includes('other')) {
      const other = (await this.ask('Enter your custom answer:', 'Enter answer · Esc dismiss')).trim();
      if (other) result.push(other);
    }
    return result.length ? result : null;
  }
}
