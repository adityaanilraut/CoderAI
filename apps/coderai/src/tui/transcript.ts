import { Markdown } from './markdown.js';
import chalk from 'chalk';
import { isAbsolute, relative, sep } from 'node:path';
import { Container, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component, type TuiMouseDispatchResult, type TuiMouseEvent } from '@coderai/pi-tui';
import type { ContextMessage, ToolInputDisplay } from '@coderai/sdk';
import { colors, markdownTheme } from './theme.js';
import { sanitizeTerminalText, TerminalSanitizer } from './sanitize.js';
import { describeToolInput } from './display.js';
import { diffStats, previewDiff, styleDiffPreview } from './diff.js';
import { todoListFromResult, TodoListComponent } from './todos.js';
import { AgentSwarmProgressComponent } from './agent-swarm-progress.js';
import { agentActivityText, type AgentActivityRecord } from './subagent-activity.js';

type Expandable = Component & { expanded: boolean };
export type ToolStart = { agentId: string; toolCallId: string; name: string; args?: unknown; description?: string; display?: ToolInputDisplay };
type ToolResult = { agentId: string; toolCallId: string; output: unknown; isError?: boolean };
type ToolState = 'running' | 'completed' | 'failed' | 'interrupted';

// Transcript markers (two cells wide so continuation rows align under the text).
const BULLET = '● ';
const USER_BULLET = '❯ ';
const FAILURE_MARK = '✗ ';
const ABORTED_MARK = '⊘ ';
const INDENT = '  ';
// Collapsed caps: outcome rows / error preview, thinking preview, and Edit/Write previews.
const OUTCOME_LINES = 3;
const THINKING_PREVIEW_LINES = 2;
const DIFF_PREVIEW_LINES = 10;
const GLANCE_SAMPLES = 3;

const stringify = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const oneLine = (text: string): string => sanitizeTerminalText(text).replace(/\s+/g, ' ').trim();
const key = (agent: string, id: string): string => JSON.stringify([agent, id]);
const dim = (text: string): string => chalk.hex(colors.textMuted)(text);
const nonEmptyLines = (text: string): string[] => text.split('\n').map(line => line.trimEnd()).filter(line => line.trim().length > 0);
const plural = (n: number, word: string, many = `${word}s`): string => `${n} ${n === 1 ? word : many}`;
const paletteKey = (): string => Object.values(colors).join();

/** Wraps plain or styled text to `width`, keeping blank rows. */
function wrap(text: string, width: number): string[] {
  const w = Math.max(1, width);
  return text.split('\n').flatMap(line => { const rows = wrapTextWithAnsi(line.replaceAll('\t', '   '), w); return rows.length ? rows : ['']; });
}
/** Prefixes the first row with a marker and the rest with the continuation indent. */
function prefixed(rows: readonly string[], first: string, rest = INDENT): string[] {
  return rows.map((row, index) => (index === 0 ? first : rest) + row);
}
/** Indented, wrapped body rows in one style. */
function bodyRows(text: string, width: number, style: (text: string) => string = dim): string[] {
  return wrap(text, width - INDENT.length).map(row => INDENT + style(row));
}
function hint(remaining: number, width: number): string {
  return INDENT + dim(truncateToWidth(`… (${plural(remaining, 'more line')}, ctrl+o to expand)`, Math.max(1, width - INDENT.length), '…'));
}
/** One dim outcome row cut to the width; the ellipsis marks the side that was cut. */
function outcomeRow(text: string, width: number, more?: 'above' | 'below'): string {
  const head = INDENT + (more === 'above' ? '… ' : ''), tail = more === 'below' ? ' …' : '';
  const room = Math.max(1, width - visibleWidth(head) - visibleWidth(tail));
  return head + dim(truncateToWidth(text, room, '…')) + dim(tail);
}
/** Short output is shown whole; longer output contributes one telling line. */
function outcomeRows(output: string, width: number, keep: 'first' | 'last'): string[] {
  const lines = nonEmptyLines(output);
  if (lines.length <= OUTCOME_LINES) return lines.map(line => outcomeRow(line, width));
  return [keep === 'first' ? outcomeRow(lines[0]!, width, 'below') : outcomeRow(lines.at(-1)!, width, 'above')];
}
function tailToWidth(text: string, width: number): string {
  const chars = Array.from(text).slice(-Math.max(1, width));
  while (chars.length && visibleWidth(chars.join('')) > width) chars.shift();
  return chars.join('');
}

/** A header row whose flexible middle (the key argument) absorbs the width cut so the label and chip survive. */
type Header = { head: string; flex?: string; keep?: 'head' | 'tail'; tail?: string };
function fitHeader({ head, flex, keep = 'head', tail = '' }: Header, width: number): string {
  if (!flex) return truncateToWidth(head + tail, width, '…');
  const room = width - visibleWidth(head) - visibleWidth(tail);
  if (room < 2) return truncateToWidth(head + tail, width, '…');
  const cut = visibleWidth(flex) <= room ? flex : keep === 'tail' ? `…${tailToWidth(flex, room - 1)}` : truncateToWidth(flex, room, '…');
  return truncateToWidth(head + dim(cut) + tail, width, '…');
}

/** Caches width-dependent rows until the width or palette changes. */
class Rows {
  private cache?: { width: number; palette: string; version: number; rows: string[] };
  version = 0;
  get(width: number, build: () => string[]): string[] {
    const palette = paletteKey();
    if (this.cache?.width === width && this.cache.palette === palette && this.cache.version === this.version) return this.cache.rows;
    const rows = build();
    this.cache = { width, palette, version: this.version, rows };
    return rows;
  }
}

export class ThinkingBlock implements Expandable {
  expanded = false;
  live = true;
  private content = '';
  private readonly sanitizer = new TerminalSanitizer();

  append(delta: string): void { this.content += this.sanitizer.write(delta); }
  invalidate(): void {}
  render(width: number): string[] {
    if (width < 1) return [];
    const text = this.content.trim();
    const style = (row: string) => chalk.hex(colors.textMuted).italic(row);
    const rows = text ? wrap(text, width - INDENT.length) : [];
    const lines = (() => {
      if (this.live) {
        const shown = this.expanded ? rows : rows.slice(-THINKING_PREVIEW_LINES);
        return ['', dim(`${BULLET}thinking…`), ...shown.map(row => INDENT + style(row))];
      }
      if (!rows.length) return [];
      const folded = !this.expanded && rows.length > THINKING_PREVIEW_LINES + 1;
      const shown = folded ? rows.slice(0, THINKING_PREVIEW_LINES) : rows;
      return ['', ...prefixed(shown.map(style), dim(BULLET)), ...(folded ? [hint(rows.length - THINKING_PREVIEW_LINES, width)] : [])];
    })();
    return lines.map(row => truncateToWidth(row, width, ''));
  }
}

/** One assistant text segment: a bullet followed by indented markdown. */
class AssistantBlock implements Component {
  readonly markdown = new Markdown('', 0, 0, markdownTheme);
  render(width: number): string[] {
    if (width < 1) return [];
    const rows = this.markdown.render(Math.max(1, width - INDENT.length));
    while (rows.length && !rows.at(-1)!.trim()) rows.pop();
    if (!rows.length) return [];
    return ['', ...prefixed(rows, BULLET)];
  }
  invalidate(): void { this.markdown.invalidate(); }
  handleMouse(event: TuiMouseEvent): TuiMouseDispatchResult | undefined {
    const width = Math.max(1, event.width - INDENT.length);
    const height = this.markdown.render(width).length;
    const y = event.y - 1;
    if (y < 0 || y >= height) return undefined;
    return this.markdown.handleMouse({ ...event, x: event.x - INDENT.length, y, width, height });
  }
}

const KEY_ARGS: Record<string, string[]> = {
  Bash: ['command'], Read: ['path', 'file_path'], Write: ['path', 'file_path'], Edit: ['path', 'file_path'],
  Grep: ['pattern'], Glob: ['pattern'], FetchURL: ['url'], WebSearch: ['query'], Agent: ['description', 'prompt'], NotifyUser: ['title', 'message'],
};
const PATH_KEYS = new Set(['path', 'file_path']);
const PARTIAL_FIELD = /"(command|path|file_path|pattern|url|query|description|title)"\s*:\s*"((?:[^"\\]|\\.)*)/g;
const FILE_CHANGE_ACK = /^(?:Replaced \d+ occurrences? in |(?:Wrote|Appended) \d+ bytes to )/;
const SEARCH_NOTICE = /^(?:Found \d+|No (?:non-sensitive )?matches|No matches collected)/;
// Expanded cards describe these inputs; file, command, search and fetch inputs are already in the header or preview.
const DESCRIBED_INPUTS = new Set<ToolInputDisplay['kind']>(['agent_call', 'skill_call', 'task', 'task_stop', 'plan_review', 'goal_start', 'generic']);

/** Reads complete string fields out of partially streamed JSON arguments. */
function partialArgs(text: string): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  for (const match of text.matchAll(PARTIAL_FIELD)) {
    try { args[match[1]!] = JSON.parse(`"${match[2]!.replace(/\\$/, '')}"`); } catch { args[match[1]!] = match[2]; }
  }
  return args;
}

class ToolCard implements Component {
  state: ToolState = 'running';
  expanded = false;
  output = '';
  private progress = '';
  private sanitizer = new TerminalSanitizer();
  private argumentsPreview = '';
  private args: Record<string, unknown> = {};
  private rawArgs = '';
  readonly agents = new Map<string, AgentActivityRecord>();
  readonly swarm = new AgentSwarmProgressComponent(this.agents);

  constructor(public call: ToolStart, private readonly workspace: string) { this.setCall(call); }
  setCall(call: ToolStart): void {
    this.call = call; this.argumentsPreview = ''; this.swarm.setArgs(call.args);
    this.args = call.args && typeof call.args === 'object' && !Array.isArray(call.args) ? call.args as Record<string, unknown> : {};
    this.rawArgs = typeof call.args === 'string' ? call.args : '';
  }
  argumentDelta(name?: string, part = ''): void {
    if (name) this.call = { ...this.call, name };
    this.argumentsPreview = (this.argumentsPreview + part).slice(0, 16_384);
    try {
      const parsed: unknown = JSON.parse(this.argumentsPreview);
      this.swarm.setArgs(parsed);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) this.args = parsed as Record<string, unknown>;
    } catch { this.args = partialArgs(this.argumentsPreview); }
  }
  update(text: string, replace = false): void {
    if (replace) { this.progress = ''; this.sanitizer = new TerminalSanitizer(); }
    this.progress = (this.progress + this.sanitizer.write(text)).slice(-16_384);
  }
  complete(output: unknown, failed = false): void {
    this.output = sanitizeTerminalText(stringify(output)); this.state = failed ? 'failed' : 'completed'; this.swarm.complete(this.output, failed);
  }
  invalidate(): void {}

  private relative(path: string): string {
    if (!this.workspace || !isAbsolute(path)) return path;
    const local = relative(this.workspace, path);
    return local && local !== '..' && !local.startsWith(`..${sep}`) && !isAbsolute(local) ? local : path;
  }
  private str(name: string): string { const value = this.args[name]; return typeof value === 'string' ? value : ''; }

  /** The header's key argument: a path keeps its file name when cut, everything else keeps its start. */
  keyArgument(): { text: string; keep: 'head' | 'tail' } | undefined {
    const name = this.call.name, display = this.call.display;
    let text = '', keep: 'head' | 'tail' = 'head';
    if (name === 'Glob' && this.str('pattern')) text = [this.str('pattern'), this.str('path') && this.relative(this.str('path'))].filter(Boolean).join(' · ');
    else if (display?.kind === 'command') text = display.command;
    else if (display?.kind === 'file_io' || display?.kind === 'diff') { text = this.relative(display.path); keep = 'tail'; }
    else if (display?.kind === 'search') text = display.query;
    else if (display?.kind === 'url_fetch') text = display.url;
    else {
      for (const field of KEY_ARGS[name] ?? Object.keys(this.args)) {
        const value = this.args[field];
        if (typeof value !== 'string' || !value) continue;
        text = PATH_KEYS.has(field) ? this.relative(value) : value; keep = PATH_KEYS.has(field) ? 'tail' : 'head';
        break;
      }
      if (!text) text = this.rawArgs || this.call.description || '';
    }
    const lines = text.split('\n');
    const first = oneLine(lines[0] ?? '');
    return first ? { text: lines.length > 1 && name === 'Bash' ? `${first}…` : first, keep } : undefined;
  }
  /** Path label used by grouped reads. */
  get label(): string { return this.keyArgument()?.text ?? this.call.name; }
  get lineCount(): number { return nonEmptyLines(this.output).length; }

  private change(): { before: string; after: string } | undefined {
    const display = this.call.display;
    if (display?.kind === 'diff') return { before: display.before, after: display.after };
    if (display?.kind === 'file_io' && display.before !== undefined && display.after !== undefined) return { before: display.before, after: display.after };
    if (this.call.name === 'Edit' && (this.str('old_string') || this.str('new_string'))) return { before: this.str('old_string'), after: this.str('new_string') };
    return undefined;
  }
  /** Short numeric stat appended to a finished card's header. */
  private chip(): string {
    if (this.state !== 'completed') return '';
    const lines = nonEmptyLines(this.output);
    switch (this.call.name) {
      case 'Read': return plural(lines.length, 'line');
      case 'Edit': {
        const change = this.change(); if (!change) return '';
        const { added, removed } = diffStats(change.before, change.after);
        return [added ? `+${added}` : '', removed ? `-${removed}` : ''].filter(Boolean).join(' ');
      }
      case 'Write': {
        const change = this.change();
        if (change) { const { added, removed } = diffStats(change.before, change.after); return [added ? `+${added}` : '', removed ? `-${removed}` : ''].filter(Boolean).join(' '); }
        const content = this.str('content').replace(/\n$/, '');
        return content ? plural(content.split('\n').length, 'line') : '';
      }
      case 'Bash': return lines.length > OUTCOME_LINES ? plural(lines.length - 1, 'more line') : '';
      case 'Grep': { const hits = lines.filter(line => !SEARCH_NOTICE.test(line)); return hits.length ? plural(hits.length, 'result') : 'no matches'; }
      case 'Glob': { const hits = lines.filter(line => !SEARCH_NOTICE.test(line)); return hits.length ? plural(hits.length, 'file') : 'no files'; }
      case 'FetchURL': { const bytes = Buffer.byteLength(this.output, 'utf8'); return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`; }
      default: return '';
    }
  }
  private header(state: ToolState): Header {
    const bullet = state === 'failed' ? chalk.hex(colors.error)(FAILURE_MARK) : state === 'completed' ? chalk.hex(colors.success)(BULLET)
      : state === 'interrupted' ? dim(ABORTED_MARK) : BULLET;
    const owner = this.call.agentId === 'main' ? '' : dim(`Agent ${oneLine(this.call.agentId)} · `);
    const arg = this.keyArgument();
    const chip = this.chip();
    const chipText = chip ? dim(` · ${chip}`) : '';
    if (this.call.name === 'Bash') {
      const label = state === 'running' ? 'Running a command' : state === 'interrupted' ? 'Interrupted command' : 'Ran a command';
      const head = `${bullet}${owner}${chalk.hex(state === 'failed' ? colors.error : colors.primary).bold(label)}`;
      return arg ? { head: head + dim(' · $ '), flex: arg.text, tail: chipText } : { head: head + chipText };
    }
    const verb = state === 'running' ? 'Using' : state === 'interrupted' ? 'Interrupted' : 'Used';
    const head = `${bullet}${owner}${verb} ${chalk.hex(colors.primary).bold(oneLine(this.call.name))}`;
    return arg ? { head: head + dim(' ('), flex: arg.text, keep: arg.keep, tail: dim(')') + chipText } : { head: head + chipText };
  }
  /** Edit diffs and Write contents, capped while collapsed. */
  private preview(width: number): string[] {
    const name = this.call.name;
    if (name !== 'Edit' && name !== 'Write') return [];
    const change = this.change();
    let rows: string[];
    if (change) rows = wrap(styleDiffPreview(previewDiff(change.before, change.after)), width - INDENT.length);
    else {
      const content = this.str('content').replace(/\n$/, '');
      if (!content) return [];
      rows = sanitizeTerminalText(content).split('\n').flatMap((line, index) => wrap(dim(String(index + 1).padStart(4) + '  ') + line, width - INDENT.length));
    }
    if (this.expanded || rows.length <= DIFF_PREVIEW_LINES + 1) return rows.map(row => INDENT + row);
    return [...rows.slice(0, DIFF_PREVIEW_LINES).map(row => INDENT + row), hint(rows.length - DIFF_PREVIEW_LINES, width)];
  }
  private result(width: number, state: ToolState): string[] {
    const name = this.call.name, output = this.output;
    if (state === 'failed') {
      const rows = bodyRows(output || 'Failed', width, text => chalk.hex(colors.error)(text));
      return this.expanded || rows.length <= OUTCOME_LINES + 1 ? rows : [...rows.slice(0, OUTCOME_LINES), hint(rows.length - OUTCOME_LINES, width)];
    }
    if (state !== 'completed' || !output.trim() || output.trimStart().startsWith('<system-reminder>')) return [];
    const ack = (name === 'Edit' || name === 'Write') && FILE_CHANGE_ACK.test(output.trim());
    if (this.expanded) return ack ? [] : bodyRows(output, width);
    if (ack || ['Read', 'FetchURL', 'WebSearch', 'Think'].includes(name)) return [];
    if (name === 'Grep' || name === 'Glob') {
      const hits = nonEmptyLines(output).filter(line => !SEARCH_NOTICE.test(line));
      if (!hits.length) return [];
      const samples = hits.slice(0, GLANCE_SAMPLES).join(', '), more = hits.length > GLANCE_SAMPLES ? `, +${hits.length - GLANCE_SAMPLES} more` : '';
      return [INDENT + fitHeader({ head: '', flex: samples, tail: dim(more) }, width - INDENT.length)];
    }
    return outcomeRows(output, width, name === 'Bash' ? 'last' : 'first');
  }
  private live(width: number, state: ToolState): string[] {
    if (state === 'completed' || state === 'failed' || !this.progress.trim()) return [];
    if (this.expanded) return bodyRows(this.progress.trimEnd(), width);
    const lines = nonEmptyLines(this.progress);
    return state === 'running' && lines.length ? [outcomeRow(lines.at(-1)!, width, lines.length > 1 ? 'above' : undefined)] : [];
  }
  render(width: number): string[] {
    if (width < 1) return [];
    const agents = [...this.agents.values()];
    const agentRunning = agents.some(agent => ['running', 'suspended', 'backgrounded'].includes(agent.status));
    const agentFailed = agents.some(agent => ['failed', 'timed_out', 'killed', 'lost'].includes(agent.status));
    const agentCancelled = agents.some(agent => ['cancelled', 'interrupted'].includes(agent.status));
    const swarmStatus = this.call.name === 'AgentSwarm' ? this.swarm.terminalStatus : undefined;
    const state: ToolState = this.state === 'failed' || agentFailed || swarmStatus === 'failed' ? 'failed'
      : swarmStatus ?? (agentRunning ? 'running' : agentCancelled ? 'interrupted' : this.state);
    const rows = [fitHeader(this.header(state), width)];
    if (this.expanded && this.call.name === 'Bash') { const command = this.str('command') || (this.call.display?.kind === 'command' ? this.call.display.command : ''); if (command) rows.push(...bodyRows(`$ ${sanitizeTerminalText(command)}`, width)); }
    if (this.expanded && this.call.display && DESCRIBED_INPUTS.has(this.call.display.kind)) rows.push(...bodyRows(sanitizeTerminalText(describeToolInput(this.call.display)), width));
    rows.push(...this.preview(width), ...this.live(width, state));
    if (this.agents.size && this.call.name !== 'AgentSwarm') {
      for (const agent of agents.sort((a, b) => (a.swarmIndex ?? 0) - (b.swarmIndex ?? 0))) {
        const lines = agentActivityText(agent, this.expanded).split('\n').filter(Boolean);
        rows.push(...bodyRows((this.expanded ? lines : [lines[0], ...lines.slice(1).slice(-2)]).join('\n'), width, text => text));
      }
    }
    rows.push(...this.result(width, state));
    this.swarm.expanded = this.expanded;
    if (this.call.name === 'AgentSwarm') rows.push(...this.swarm.render(Math.max(1, width - 1)).map(row => ' ' + row));
    return rows.map(row => truncateToWidth(row, width, ''));
  }
}

/** One tool card, or 2+ consecutive reads folded into a single summary row. */
class ToolGroup implements Expandable {
  expanded = false;
  readonly cards: ToolCard[] = [];
  constructor(readonly agentId: string) {}
  invalidate(): void { for (const card of this.cards) card.invalidate(); }
  render(width: number): string[] {
    if (width < 1 || !this.cards.length) return [];
    if (this.cards.length === 1) { this.cards[0]!.expanded = this.expanded; return ['', ...this.cards[0]!.render(width)]; }
    const total = this.cards.length;
    const running = this.cards.filter(card => card.state === 'running').length;
    const failed = this.cards.filter(card => card.state === 'failed').length;
    const interrupted = this.cards.filter(card => card.state === 'interrupted').length;
    const lines = this.cards.reduce((sum, card) => sum + (card.state === 'completed' ? card.lineCount : 0), 0);
    const owner = this.agentId === 'main' ? '' : dim(`Agent ${oneLine(this.agentId)} · `);
    let header: Header;
    if (running) header = { head: `${BULLET}${owner}${chalk.hex(colors.primary).bold(`Reading ${total} files…`)}`, tail: failed ? chalk.hex(colors.error)(` · ${failed} failed`) : '' };
    else if (failed === total) header = { head: `${chalk.hex(colors.error)(FAILURE_MARK)}${owner}${chalk.hex(colors.error).bold(`Read ${total} files`)}${chalk.hex(colors.error)(' · failed')}` };
    else header = {
      head: `${chalk.hex(colors.success)(BULLET)}${owner}${chalk.hex(colors.primary).bold(`Read ${total} files`)}`,
      flex: ` · ${plural(lines, 'line')}`,
      tail: (interrupted ? dim(` · ${interrupted} interrupted`) : '') + (failed ? chalk.hex(colors.error)(` · ${failed} failed`) : ''),
    };
    // Collapsed groups list only failed reads; ctrl+o lists every file.
    const shown = this.cards.filter(card => this.expanded || card.state === 'failed');
    const tree = shown.flatMap((card, index) => {
      const status = card.state === 'running' ? dim(' · reading…') : card.state === 'failed' ? chalk.hex(colors.error)(' · failed')
        : card.state === 'interrupted' ? dim(' · interrupted') : dim(` · ${plural(card.lineCount, 'line')}`);
      const line = fitHeader({ head: dim(`${INDENT}${index === shown.length - 1 ? '└─' : '├─'} `), flex: card.label, keep: 'tail', tail: status }, width);
      const body = card.state === 'failed'
        ? bodyRows(card.output || 'Failed', width - INDENT.length, text => chalk.hex(colors.error)(text)).slice(0, this.expanded ? undefined : OUTCOME_LINES).map(row => INDENT + row)
        : this.expanded && card.output.trim() ? bodyRows(card.output, width - INDENT.length).map(row => INDENT + row) : [];
      return [line, ...body];
    });
    return ['', fitHeader(header, width), ...tree].map(row => truncateToWidth(row, width, ''));
  }
}

export class ConversationTranscript extends Container {
  private assistant?: AssistantBlock;
  private assistantText = '';
  private assistantSanitizer = new TerminalSanitizer();
  private thinking?: ThinkingBlock;
  private readGroup?: ToolGroup;
  private readonly cards = new Map<string, ToolCard>();
  private readonly pending = new Map<string, ToolCard>();
  private readonly replayTodoCalls = new Set<string>();
  private readonly expandable: Expandable[] = [];
  private expanded = false;
  private workspace = '';
  lastAssistantText = '';

  get hasDetails(): boolean { return this.expandable.length > 0; }
  setWorkspace(workspace: string): void { this.workspace = workspace; }
  override render(width: number): string[] {
    if (width < 1) return [];
    if (width < 3) return super.render(3).map(line => truncateToWidth(line.trimStart(), width, ''));
    return super.render(width);
  }
  setExpanded(expanded: boolean): void {
    this.expanded = expanded;
    for (const block of this.expandable) block.expanded = expanded;
  }
  breakGroup(): void { this.readGroup = undefined; }
  override clear(): void {
    super.clear(); this.pending.clear(); this.cards.clear(); this.replayTodoCalls.clear(); this.expandable.length = 0; this.readGroup = undefined;
    this.beginAssistant();
  }
  private endAssistant(): void {
    this.assistant?.markdown.setTransient(false); this.assistant = undefined; this.assistantText = ''; this.assistantSanitizer = new TerminalSanitizer();
  }
  beginAssistant(newTurn = true): void {
    this.finishThinking(); this.endAssistant();
    if (newTurn) { this.lastAssistantText = ''; this.breakGroup(); }
  }
  userMessage(text: string): void {
    this.beginAssistant();
    const clean = sanitizeTerminalText(text);
    const rows = new Rows();
    this.addChild({
      render: width => rows.get(width, () => {
        const style = (row: string) => chalk.hex(colors.user).bold(row);
        return ['', ...prefixed(wrap(clean, width - INDENT.length).map(style), style(USER_BULLET))].map(row => truncateToWidth(row, width, ''));
      }),
      invalidate: () => { rows.version++; },
    });
  }
  delta(text: string): void {
    this.finishThinking(); this.breakGroup();
    if (!this.assistant) {
      this.assistant = new AssistantBlock(); this.assistant.markdown.setTransient(true); this.addChild(this.assistant);
    }
    const clean = this.assistantSanitizer.write(text);
    this.assistantText += clean; this.lastAssistantText += clean; this.assistant.markdown.setText(this.assistantText);
  }
  thinkingDelta(text: string): void {
    this.breakGroup();
    if (!this.thinking) {
      this.endAssistant();
      this.thinking = new ThinkingBlock(); this.thinking.expanded = this.expanded;
      this.expandable.push(this.thinking); this.addChild(this.thinking);
    }
    this.thinking.append(text);
  }
  finishThinking(): void { if (this.thinking) this.thinking.live = false; this.thinking = undefined; }
  toolStarted(call: ToolStart): void {
    if (call.agentId === 'main') this.beginAssistant(false);
    const id = key(call.agentId, call.toolCallId);
    const existing = this.pending.get(id);
    if (existing) { existing.setCall(call); return; }
    const card = new ToolCard(call, this.workspace);
    const reads = call.name === 'Read';
    let group = reads && this.readGroup?.agentId === call.agentId ? this.readGroup : undefined;
    if (!group) {
      group = new ToolGroup(call.agentId); group.expanded = this.expanded;
      this.expandable.push(group); this.addChild(group);
    }
    this.readGroup = reads ? group : undefined;
    group.cards.push(card); this.pending.set(id, card); this.cards.set(id, card);
  }
  hideTool(agentId: string, toolCallId: string): void {
    const id = key(agentId, toolCallId); const card = this.cards.get(id);
    if (!card) return;
    for (const component of this.children) if (component instanceof ToolGroup) {
      const index = component.cards.indexOf(card); if (index >= 0) component.cards.splice(index, 1);
    }
    this.pending.delete(id); this.cards.delete(id);
  }
  toolDelta(event: { agentId: string; toolCallId: string; name?: string; argumentsPart?: string }): void {
    if (!event.toolCallId) return;
    const id = key(event.agentId, event.toolCallId);
    if (!this.pending.has(id)) this.toolStarted({ ...event, name: event.name ?? 'Tool' });
    this.pending.get(id)!.argumentDelta(event.name, event.argumentsPart);
  }
  agentActivity(record: AgentActivityRecord): void {
    this.cards.get(key(record.parentAgentId, record.parentToolCallId))?.agents.set(record.agentId, record);
  }
  toolProgress(agentId: string, toolCallId: string, text: string, replace = false): void { this.pending.get(key(agentId, toolCallId))?.update(text, replace); }
  toolResult(result: ToolResult): void {
    const id = key(result.agentId, result.toolCallId);
    if (!this.pending.has(id)) this.toolStarted({ agentId: result.agentId, toolCallId: result.toolCallId, name: 'Result', description: result.toolCallId });
    this.pending.get(id)!.complete(result.output, result.isError); this.pending.delete(id);
  }
  finishTurn(agentId = 'main'): void {
    if (agentId === 'main') { this.assistant?.markdown.setTransient(false); this.finishThinking(); this.breakGroup(); }
    for (const [id, card] of this.pending) {
      if (card.call.agentId !== agentId) continue;
      card.state = 'interrupted'; card.swarm.interrupt(); this.pending.delete(id);
    }
  }
  replayMessage(message: ContextMessage): void {
    if (message.role === 'system' || message.origin?.kind === 'injection') return;
    const parts = typeof message.content === 'string' ? [{ type: 'text' as const, text: message.content }] : message.content;
    const text = parts.map(part => part.type === 'text' ? part.text : part.type === 'think' ? '' : `[${part.type} attachment]`).join('');
    if (message.role === 'tool') {
      const todoCall = this.replayTodoCalls.delete(message.toolCallId ?? '');
      const todos = !message.isError ? todoListFromResult(text) : undefined;
      if (todos !== undefined) {
        const card = new TodoListComponent(); card.setItems(todos); card.expanded = this.expanded;
        this.expandable.push(card); this.addChild(card); this.breakGroup();
      } else {
        if (todoCall) this.toolStarted({ agentId: 'main', toolCallId: message.toolCallId ?? '', name: 'TodoList' });
        this.toolResult({ agentId: 'main', toolCallId: message.toolCallId ?? '', output: text, isError: message.isError });
      }
      return;
    }
    if (message.role === 'user') { this.finishTurn(); this.userMessage(text); return; }
    this.beginAssistant(false);
    for (const part of parts) {
      if (part.type === 'think' && !part.hidden) this.thinkingDelta(part.think);
      else if (part.type === 'text' && part.text) this.delta(part.text);
      else if (part.type !== 'think' && part.type !== 'text') this.delta(`[${part.type} attachment]`);
    }
    this.finishThinking();
    for (const call of message.toolCalls ?? []) {
      if (call.name === 'TodoList') { this.replayTodoCalls.add(call.id); continue; }
      let args: unknown = call.arguments;
      try { args = JSON.parse(call.arguments ?? '{}'); } catch { /* Older transcripts can contain incomplete arguments. */ }
      this.toolStarted({ agentId: 'main', toolCallId: call.id, name: call.name, args, display: message.toolCallDisplays?.[call.id] });
    }
  }
}
