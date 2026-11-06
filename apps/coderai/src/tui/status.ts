import type { TuiConfig } from './config.js';
import chalk from 'chalk';
import { truncateToWidth, visibleWidth, type Component } from '@coderai/pi-tui';
import type { SessionStatus, TurnStepRetryingEvent } from '@coderai/sdk';
import { colors } from './theme.js';
import { sanitizeTerminalText } from './sanitize.js';
import type { GitStatus } from './git-status.js';

export const singleLine = (text: string): string => sanitizeTerminalText(text).replace(/\s+/g, ' ').trim();

function tokens(value: number): string {
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(1))}m`;
  if (value >= 1_000) return `${Number((value / 1_000).toFixed(1))}k`;
  return String(Math.round(value));
}

export class StatusFooter implements Component {
  private preferences: TuiConfig['statusLine'] = { items: null, command: '' };
  private goalStatus = '';
  private taskCount = 0;
  setActivity(goalStatus: string, taskCount: number): void { this.goalStatus = goalStatus; this.taskCount = taskCount; }
  private customLine: string | undefined;
  setPreferences(preferences: TuiConfig['statusLine']): void { this.preferences = preferences; this.customLine = undefined; }
  setCustomLine(line: string | undefined): void { this.customLine = line === undefined ? undefined : singleLine(line); }
  private state?: SessionStatus;
  private workspace = '';
  private model = '';
  private attachments = 0;
  private expandHint = '';
  private title = '';
  setTitle(title: string): void { this.title = singleLine(title); }
  private git: GitStatus | null = null;

  setGitStatus(status: GitStatus | null): void { this.git = status; }

  setExpandHint(expandable: boolean, expanded: boolean): void {
    this.expandHint = expandable ? `Ctrl+O ${expanded ? 'collapse' : 'expand'}` : '';
  }

  setState(state: SessionStatus, workspace: string, model: string, attachments = 0): void {
    this.state = state; this.workspace = singleLine(workspace); this.model = singleLine(model); this.attachments = attachments;
  }

  invalidate(): void {}

  render(width: number): string[] {
    if (!this.state || width < 3) return [];
    const available = width - 2;
    const state = this.state;
    const badges = [
      state.planMode ? chalk.hex(colors.primary).bold('[PLAN]') : '',
      state.permission === 'yolo' ? chalk.hex(colors.warning).bold('[YOLO]') : '',
      state.permission === 'auto' ? chalk.hex(colors.warning).bold('[AUTO]') : '',
      state.swarmMode ? chalk.hex(colors.primary)('[SWARM]') : '',
      state.towerMode ? chalk.hex(colors.primary)('[TOWER]') : '',
    ].filter(Boolean).join(' ');
    const prefix = badges ? badges + ' ' : '';
    const cwd = chalk.hex(colors.textMuted)(this.workspace);
    const git = this.git ? chalk.hex(this.git.dirty ? colors.warning : colors.textMuted)(
      `git:${truncateToWidth(singleLine(this.git.branch), Math.max(1, Math.min(Math.floor(available / 3), available - visibleWidth(prefix) - 18)), '…')} ${this.git.dirty ? 'dirty' : 'clean'}`) : '';
    const budget = Math.max(0, available - visibleWidth(prefix) - (git ? visibleWidth(git) + 3 : 0));
    let model = truncateToWidth(this.model || 'No model', budget, '…');
    if (visibleWidth(model) + visibleWidth(cwd) + 3 <= budget) model += ` · ${cwd}`;
    if (state.thinkingEffort !== 'off' && state.thinkingEffort && visibleWidth(model) + state.thinkingEffort.length + 10 <= budget) {
      model += chalk.hex(colors.textMuted)(` · effort ${singleLine(state.thinkingEffort)}`);
    }
    const knownLimit = Number.isFinite(state.maxContextTokens) && state.maxContextTokens > 0 && Number.isFinite(state.contextTokens) && state.contextTokens >= 0;
    const percentage = knownLimit ? Math.max(0, Math.round(state.contextTokens / state.maxContextTokens * 100)) : undefined;
    const context = percentage === undefined ? 'Context unavailable' : `Context ${percentage}%`;
    const count = knownLimit ? ` · ${tokens(state.contextTokens)}/${tokens(state.maxContextTokens)}` : '';
    const attachment = this.attachments ? ` · ${this.attachments} attachment${this.attachments === 1 ? '' : 's'}` : '';
    const contextColor = percentage !== undefined && percentage >= 90 ? colors.warning : colors.textMuted;
    let second = context + attachment;
    if (visibleWidth(second + count) <= available) second = context + count + attachment;
    const titleWidth = available - visibleWidth(second) - 3;
    if (this.title && titleWidth >= 8) second += ` · ${truncateToWidth(this.title, titleWidth, '…')}`;
    const hint = this.expandHint;
    const padding = available - visibleWidth(second) - visibleWidth(hint);
    if (hint && padding >= 2) second = hint + ' '.repeat(padding) + second;
    const slots: Record<string, string> = { mode: badges || state.permission, model: this.model, cwd: this.workspace, git, goal: this.goalStatus ? `Goal: ${this.goalStatus}` : '', tasks: this.taskCount ? `${this.taskCount} running tasks` : '', tips: this.expandHint };
    const first = this.customLine ?? (this.preferences.items ? this.preferences.items.map(item => slots[item]).filter(Boolean).join(' · ') : prefix + model + (git ? ` · ${git}` : ''));
    return [
      ' ' + truncateToWidth(first, available, '…'),
      ' ' + chalk.hex(contextColor)(truncateToWidth(second, available, '…')),
    ];
  }
}

type Retry = Pick<TurnStepRetryingEvent, 'nextAttempt' | 'maxAttempts' | 'delayMs' | 'errorName' | 'errorMessage' | 'statusCode'>;
const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

export class ActivityIndicator implements Component {
  private label = 'Starting…';
  private detail = '';
  private since = 0;
  private active = false;
  private enabled = false;
  private interval?: ReturnType<typeof setInterval>;
  private retry?: Retry & { at: number };

  constructor(private readonly refresh: () => void, private readonly now = Date.now) {}

  start(): void { this.enabled = true; this.syncTimer(); }
  stop(): void { this.enabled = false; this.syncTimer(); }
  invalidate(): void {}

  setText(label: string): void {
    this.active = false; this.retry = undefined; this.detail = ''; this.label = singleLine(label); this.syncTimer();
  }

  setActivity(label: string, detail = ''): void {
    if (!this.active) this.since = this.now();
    this.active = true; this.label = singleLine(label); this.detail = singleLine(detail); this.retry = undefined; this.syncTimer();
  }

  setRetry(retry: Retry): void {
    this.setActivity('Retrying model request');
    this.retry = { ...retry, at: this.now() + Math.max(0, retry.delayMs) };
    let message = singleLine(retry.errorMessage || retry.errorName);
    if (retry.statusCode !== undefined) message = message.replace(new RegExp(`^${retry.statusCode}[:\\s]+`), '');
    this.detail = [retry.statusCode, message].filter(value => value !== undefined && value !== '').join(' · ');
  }

  private syncTimer(): void {
    if (this.enabled && this.active && !this.interval) {
      this.interval = setInterval(() => this.refresh(), 120);
      this.interval.unref();
    } else if ((!this.enabled || !this.active) && this.interval) {
      clearInterval(this.interval); this.interval = undefined;
    }
  }

  render(width: number): string[] {
    if (width < 3) return [];
    const available = width - 2;
    if (!this.active) return [' ' + chalk.hex(colors.textMuted)(truncateToWidth(this.label, available, '…'))];
    const now = this.now();
    const seconds = Math.max(0, Math.floor((now - this.since) / 1000));
    const elapsed = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    let label = this.label;
    if (this.retry) {
      const wait = Math.max(0, Math.ceil((this.retry.at - now) / 1000));
      label = `Retrying (${this.retry.nextAttempt}/${this.retry.maxAttempts})${wait ? ` · in ${wait}s` : ' · requesting model'}`;
    }
    const spinner = frames[Math.floor(now / 120) % frames.length];
    const suffix = ` · ${elapsed}`;
    const hint = ' · Esc cancel';
    const line = `${spinner} ${label}`;
    const withHint = visibleWidth(line + suffix + hint) <= available;
    const end = suffix + (withHint ? hint : '');
    const rows = [' ' + chalk.hex(colors.primary)(truncateToWidth(line, Math.max(0, available - visibleWidth(end)), '…') + end)];
    if (this.detail) rows.push(' ' + chalk.hex(colors.textMuted)(truncateToWidth(this.detail, available, '…')));
    return rows.map(row => truncateToWidth(row, width, '…'));
  }
}
