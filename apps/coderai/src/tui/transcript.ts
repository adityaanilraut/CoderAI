import { Markdown } from './markdown.js';
import chalk from 'chalk';
import { isAbsolute, relative, sep } from 'node:path';
import { Container,  Text, truncateToWidth, type Component } from '@coderai/pi-tui';
import type { ContextMessage, ToolInputDisplay } from '@coderai/sdk';
import { colors, markdownTheme } from './theme.js';
import { sanitizeTerminalText, TerminalSanitizer } from './sanitize.js';
import { describeToolInput } from './display.js';
import { styleDiffPreview } from './diff.js';
import { todoListFromResult, TodoListComponent } from './todos.js';
import { AgentSwarmProgressComponent } from './agent-swarm-progress.js';
import { agentActivityText, type AgentActivityRecord } from './subagent-activity.js';

type Expandable = Component & { expanded: boolean };
export type ToolStart = { agentId: string; toolCallId: string; name: string; args?: unknown; description?: string; display?: ToolInputDisplay };
type ToolResult = { agentId: string; toolCallId: string; output: unknown; isError?: boolean };
const stringify = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const oneLine = (text: string): string => sanitizeTerminalText(text).replace(/\s+/g, ' ').trim();
const key = (agent: string, id: string): string => JSON.stringify([agent, id]);

export class ThinkingBlock implements Expandable {
  expanded = false;
  live = true;
  private content = '';
  private readonly sanitizer = new TerminalSanitizer();
  private readonly text = new Text('', 1, 0);

  append(delta: string): void { this.content += this.sanitizer.write(delta); this.text.setText(this.content); }
  invalidate(): void { this.text.invalidate(); }
  render(width: number): string[] {
    if (width < 1) return [];
    const label = this.live ? 'Thinking…' : 'Thinking';
    const header = truncateToWidth(` ${label} · Ctrl+O ${this.expanded ? 'collapse' : 'expand'}`, width, '…');
    const lines = this.expanded || this.live ? this.text.render(width) : [];
    return [chalk.hex(colors.textMuted)(header), ...(this.expanded ? lines : lines.slice(-3)).map(line => chalk.hex(colors.textMuted).italic(line))];
  }
}

class ToolCard implements Component {
  state: 'running' | 'completed' | 'failed' | 'interrupted' = 'running';
  expanded = false;
  output = '';
  private progress = '';
  private sanitizer = new TerminalSanitizer();
  private readonly body = new Text('', 1, 0);
  private bodyText = '';
  private inputPalette = '';
  private styledInput = '';
  target = '';
  input = '';
  private argumentsPreview = '';
  readonly agents = new Map<string, AgentActivityRecord>();
  readonly swarm = new AgentSwarmProgressComponent(this.agents);

  constructor(public call: ToolStart, private readonly workspace: string) { this.setCall(call); }
  setCall(call: ToolStart): void {
    this.call = call; this.argumentsPreview = ''; this.inputPalette = ''; this.swarm.setArgs(call.args);
    const workspace = this.workspace;
    const display = call.display;
    const args = call.args && typeof call.args === 'object' ? call.args as Record<string, unknown> : {};
    let target = display?.kind === 'file_io' || display?.kind === 'diff' ? display.path
      : display?.kind === 'command' ? `$ ${display.command}`
      : display?.kind === 'search' ? display.query
      : display?.kind === 'url_fetch' ? display.url
      : typeof args['path'] === 'string' ? args['path']
      : typeof args['command'] === 'string' ? `$ ${args['command']}` : call.description ?? '';
    if (workspace && isAbsolute(target)) {
      const local = relative(workspace, target);
      if (local !== '..' && !local.startsWith(`..${sep}`) && !isAbsolute(local)) target = local || '.';
    }
    this.target = oneLine(target);
    this.input = sanitizeTerminalText(display ? describeToolInput(display) : stringify(call.args));
  }
  argumentDelta(name?: string, part = ''): void {
    if (name) this.call = { ...this.call, name };
    this.argumentsPreview = (this.argumentsPreview + part).slice(0, 16_384);
    this.input = sanitizeTerminalText(this.argumentsPreview); this.inputPalette = '';
    try { this.swarm.setArgs(JSON.parse(this.argumentsPreview)); } catch { /* The raw preview remains visible until JSON is complete. */ }
  }
  update(text: string, replace = false): void {
    if (replace) { this.progress = ''; this.sanitizer = new TerminalSanitizer(); }
    this.progress = (this.progress + this.sanitizer.write(text)).slice(-16_384);
  }
  complete(output: unknown, failed = false): void {
    this.output = sanitizeTerminalText(stringify(output)); this.state = failed ? 'failed' : 'completed'; this.swarm.complete(this.output, failed);
  }
  invalidate(): void { this.body.invalidate(); }
  render(width: number): string[] {
    if (width < 1) return [];
    const agents = [...this.agents.values()];
    const agentRunning = agents.some(agent => ['running', 'suspended', 'backgrounded'].includes(agent.status));
    const agentFailed = agents.some(agent => ['failed', 'timed_out', 'killed', 'lost'].includes(agent.status));
    const agentCancelled = agents.some(agent => ['cancelled', 'interrupted'].includes(agent.status));
    const swarmStatus = this.call.name === 'AgentSwarm' ? this.swarm.terminalStatus : undefined;
    const state = this.state === 'failed' || agentFailed || swarmStatus === 'failed' ? 'failed'
      : swarmStatus ?? (agentRunning ? 'running' : agentCancelled ? 'interrupted' : this.state);
    const failed = state === 'failed';
    const color = failed ? colors.error : state === 'completed' ? colors.success : colors.textMuted;
    const symbol = failed ? '✗' : state === 'completed' ? '✓' : state === 'running' ? '●' : '○';
    const owner = this.call.agentId === 'main' ? '' : `Agent ${oneLine(this.call.agentId)} · `;
    const label = state === 'running' ? 'Tool' : `Tool ${state}`;
    const title = `${symbol} ${owner}${label}: ${oneLine(this.call.name)}${this.target ? ` · ${this.target}` : ''}`;
    const header = chalk.hex(color)(truncateToWidth(` ${title}`, width, '…'));
    let body = '';
    if (this.expanded) {
      const palette = `${colors.success}/${colors.error}/${colors.primary}`;
      if (palette !== this.inputPalette) { this.inputPalette = palette; this.styledInput = styleDiffPreview(this.input); }
      body = [this.input && `Input\n${this.styledInput}`, this.output && `Output\n${this.output}`, !this.output && this.progress].filter(Boolean).join('\n');
    }
    else if (failed) body = this.output;
    else if (this.state === 'running') body = this.progress || this.input || (this.call.display?.kind === 'command' ? `$ ${this.call.display.command}` : '');
    if (this.agents.size && this.call.name !== 'AgentSwarm') {
      const agents = [...this.agents.values()].sort((a, b) => (a.swarmIndex ?? 0) - (b.swarmIndex ?? 0));
      const details = agents.map(agent => {
        const text = agentActivityText(agent, this.expanded);
        const lines = text.split('\n').filter(Boolean);
        return this.expanded ? text : [lines[0], ...lines.slice(1).slice(-2)].join('\n');
      });
      body = [body, ...details].filter(Boolean).join('\n');
    }
    if (body !== this.bodyText) { this.bodyText = body; this.body.setText(body); }
    const lines = this.body.render(width);
    const preview = this.expanded || this.agents.size ? lines : failed ? lines.slice(0, 3) : lines.slice(-3);
    this.swarm.expanded = this.expanded;
    const swarm = this.call.name === 'AgentSwarm' ? this.swarm.render(width) : [];
    return [header, ...preview, ...swarm, ...(failed && !this.expanded && lines.length > 3 ? [truncateToWidth(' … Ctrl+O to expand error', width, '…')] : [])];
  }
}

class ToolGroup implements Expandable {
  expanded = false;
  readonly cards: ToolCard[] = [];
  constructor(readonly agentId: string) {}
  invalidate(): void { for (const card of this.cards) card.invalidate(); }
  render(width: number): string[] {
    if (width < 1 || !this.cards.length) return [];
    if (this.cards.length === 1) { this.cards[0]!.expanded = this.expanded; return this.cards[0]!.render(width); }
    const running = this.cards.filter(card => card.state === 'running').length;
    const failed = this.cards.filter(card => card.state === 'failed').length;
    const interrupted = this.cards.filter(card => card.state === 'interrupted').length;
    const owner = this.agentId === 'main' ? '' : `Agent ${oneLine(this.agentId)} · `;
    const label = `${running ? '●' : failed ? '✗' : interrupted ? '○' : '✓'} ${owner}${running ? 'Reading' : 'Read'} ${this.cards.length} files`;
    const status = running ? `${running} running` : interrupted ? `${interrupted} interrupted` : 'completed';
    const header = chalk.hex(failed ? colors.error : running || interrupted ? colors.textMuted : colors.success)(truncateToWidth(` ${label} · ${status}${failed ? ` · ${failed} failed` : ''} · Ctrl+O ${this.expanded ? 'collapse' : 'expand'}`, width, '…'));
    return [header, ...this.cards.flatMap(card => {
      card.expanded = this.expanded;
      return this.expanded || card.state === 'failed' ? card.render(width) : [];
    })];
  }
}

export class ConversationTranscript extends Container {
  private assistant?: Markdown;
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
  beginAssistant(newTurn = true): void {
    this.finishThinking(); this.assistant?.setTransient(false); this.assistant = undefined; this.assistantText = ''; this.assistantSanitizer = new TerminalSanitizer();
    if (newTurn) { this.lastAssistantText = ''; this.breakGroup(); }
  }
  userMessage(text: string): void {
    this.beginAssistant();
    const clean = sanitizeTerminalText(text);
    const body = new Text('', 1, 1);
    let styled = '';
    this.addChild({
      render: width => {
        const next = chalk.hex(colors.primary).bold(`You: ${clean}`);
        if (next !== styled) { styled = next; body.setText(styled); }
        return body.render(width);
      },
      invalidate: () => body.invalidate(),
    });
  }
  delta(text: string): void {
    this.finishThinking(); this.breakGroup();
    if (!this.assistant) {
      this.addChild({ render: width => ['', truncateToWidth(chalk.hex(colors.primary).bold(' CoderAI:'), width, '…')], invalidate: () => {} });
      this.assistant = new Markdown('', 1, 0, markdownTheme); this.assistant.setTransient(true); this.addChild(this.assistant);
    }
    const clean = this.assistantSanitizer.write(text);
    this.assistantText += clean; this.lastAssistantText += clean; this.assistant.setText(this.assistantText);
  }
  thinkingDelta(text: string): void {
    this.breakGroup();
    if (!this.thinking) {
      this.assistant?.setTransient(false); this.assistant = undefined; this.assistantText = ''; this.assistantSanitizer = new TerminalSanitizer();
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
    if (agentId === 'main') { this.assistant?.setTransient(false); this.finishThinking(); this.breakGroup(); }
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
