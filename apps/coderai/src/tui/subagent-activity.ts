import type { Event } from '@coderai/sdk';
import { sanitizeTerminalText } from './sanitize.js';

export const ACTIVITY_STEPS = 20;
export const ACTIVITY_TEXT_LIMIT = 16_384;
export const ACTIVITY_OUTPUT_LIMIT = 8_192;
export interface AgentToolActivity {
  id: string; name: string; args: string; status: string; output: string; progress: string;
}
export interface AgentStepActivity { step: number; text: string; thinking: string; tools: AgentToolActivity[] }
export interface AgentActivityRecord {
  agentId: string; name: string; parentToolCallId: string; parentAgentId: string; description?: string;
  background: boolean; swarmIndex?: number; model?: string; effort?: string; contextTokens?: number;
  status: string; summary?: string; steps: AgentStepActivity[]; totalSteps: number;
}
const serialize = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';

/** A bounded fold of child events, independent of whether the parent tool is mounted. */
export class SubagentActivityStore {
  private readonly records = new Map<string, AgentActivityRecord>();
  private readonly listeners = new Set<() => void>();
  get(id: string): AgentActivityRecord | undefined { return this.records.get(id); }
  all(): AgentActivityRecord[] { return [...this.records.values()]; }
  subscribe(listener: () => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  clear(): void { this.records.clear(); this.changed(); }
  private changed(): void { for (const listener of this.listeners) listener(); }
  private ensure(id: string): AgentActivityRecord {
    let record = this.records.get(id);
    if (!record) {
      record = { agentId: id, name: id, parentToolCallId: '', parentAgentId: 'main', background: false, status: 'running', steps: [], totalSteps: 0 };
      this.records.set(id, record);
    }
    return record;
  }
  private step(record: AgentActivityRecord): AgentStepActivity {
    if (!record.steps.length) record.steps.push({ step: 0, text: '', thinking: '', tools: [] });
    return record.steps.at(-1)!;
  }
  private call(record: AgentActivityRecord, id: string): AgentToolActivity {
    for (const step of record.steps.toReversed()) {
      const call = step.tools.find(call => call.id === id);
      if (call) return call;
    }
    const call = { id, name: '', args: '', status: 'running', output: '', progress: '' };
    const step = this.step(record); step.tools.push(call);
    if (step.tools.length > 100) step.tools.shift();
    return call;
  }
  private terminal(record: AgentActivityRecord, status: string, summary?: string): void {
    record.status = status;
    if (summary !== undefined) record.summary = summary.slice(0, ACTIVITY_OUTPUT_LIMIT);
    for (const step of record.steps) for (const call of step.tools) if (call.status === 'running') call.status = 'interrupted';
  }
  apply(event: Event): AgentActivityRecord | undefined {
    let record: AgentActivityRecord;
    if (event.type === 'subagent.spawned') {
      record = this.ensure(event.subagentId);
      Object.assign(record, { name: event.subagentName, parentToolCallId: event.parentToolCallId,
        parentAgentId: event.parentAgentId ?? event.callerAgentId ?? event.agentId, background: event.runInBackground,
        description: event.description, swarmIndex: event.swarmIndex, model: event.model, effort: event.thinkingEffort, status: 'running', summary: undefined });
    } else if (event.type.startsWith('subagent.') && 'subagentId' in event) {
      record = this.ensure(event.subagentId);
      switch (event.type) {
        case 'subagent.started': record.status = 'running'; break;
        case 'subagent.suspended': record.status = 'suspended'; record.summary = event.reason; break;
        case 'subagent.completed':
          // A task termination may carry a more precise failure state.
          this.terminal(record, ['running', 'suspended', 'backgrounded', 'completed'].includes(record.status) ? 'completed' : record.status, event.resultSummary); break;
        case 'subagent.failed': this.terminal(record, 'failed', event.error); break;
        case 'subagent.cancelled': this.terminal(record, 'cancelled'); break;
      }
    } else if ((event.type === 'background.task.started' || event.type === 'background.task.terminated') && event.info.kind === 'agent' && event.info.agentId) {
      record = this.ensure(event.info.agentId); record.background = true;
      record.description ||= event.info.description;
      if (event.type === 'background.task.started') record.status = 'backgrounded';
      else this.terminal(record, event.info.status);
    } else {
      if (event.agentId === 'main' || event.type.startsWith('subagent.')) return;
      record = this.ensure(event.agentId);
      switch (event.type) {
        case 'turn.step.started':
          record.steps.push({ step: event.step, text: '', thinking: '', tools: [] }); record.totalSteps++;
          if (record.steps.length > ACTIVITY_STEPS) record.steps.shift(); break;
        case 'assistant.delta': { const step = this.step(record); step.text = (step.text + event.delta).slice(-ACTIVITY_TEXT_LIMIT); break; }
        case 'thinking.delta': { const step = this.step(record); step.thinking = (step.thinking + event.delta).slice(-ACTIVITY_TEXT_LIMIT); break; }
        case 'hook.result': { const step = this.step(record); step.text = (step.text + `\n${event.hookEvent} hook${event.blocked ? ' blocked' : ''}\n${event.content.trim() || '(empty)'}`).slice(-ACTIVITY_TEXT_LIMIT); break; }
        case 'tool.call.delta': {
          const call = this.call(record, event.toolCallId); call.name ||= event.name ?? '';
          call.args = (call.args + (event.argumentsPart ?? '')).slice(0, ACTIVITY_TEXT_LIMIT); break;
        }
        case 'tool.call.started': {
          const call = this.call(record, event.toolCallId); call.name = event.name;
          call.args = serialize(event.args).slice(0, ACTIVITY_TEXT_LIMIT); break;
        }
        case 'tool.progress': {
          const call = this.call(record, event.toolCallId);
          call.progress = ((event.update.replace ? '' : call.progress) + (event.update.text ?? '')).slice(-2048); break;
        }
        case 'tool.result': {
          const call = this.call(record, event.toolCallId); call.status = event.isError ? 'failed' : 'completed';
          call.output = serialize(event.output).slice(0, ACTIVITY_OUTPUT_LIMIT); call.progress = ''; break;
        }
        case 'agent.status.updated':
          record.model = event.model ?? record.model; record.effort = event.thinkingEffort ?? record.effort;
          record.contextTokens = event.contextTokens ?? record.contextTokens; break;
        case 'turn.step.retrying': record.summary = `Retrying ${event.nextAttempt}/${event.maxAttempts}: ${event.errorMessage}`; break;
        case 'turn.step.interrupted':
          this.terminal(record, 'interrupted', event.message ?? event.reason); break;
        case 'turn.ended':
          if (event.reason !== 'completed') this.terminal(record, event.reason, event.error?.message); break;
        default: return;
      }
    }
    // Keep completed history bounded across long sessions; running tasks are retained.
    if (this.records.size > 128) {
      const oldest = [...this.records.values()].find(item => item !== record && !['running', 'suspended', 'backgrounded'].includes(item.status));
      if (oldest) this.records.delete(oldest.agentId);
    }
    this.changed(); return record;
  }
  finishForeground(): void {
    for (const record of this.records.values()) if (!record.background && ['running', 'suspended'].includes(record.status)) this.terminal(record, 'interrupted');
    this.changed();
  }
}

export function agentActivityText(record: AgentActivityRecord, expanded = true): string {
  const metrics = [record.model, record.effort, record.contextTokens === undefined ? '' : `${record.contextTokens} context tokens`].filter(Boolean).join(' · ');
  const steps = expanded ? record.steps : record.steps.slice(-1);
  return sanitizeTerminalText([`${record.name}${record.description ? ` · ${record.description}` : ''} · ${record.status}${metrics ? ` · ${metrics}` : ''}`,
    ...steps.flatMap(step => [`Step ${step.step}`, step.thinking && `Thinking: ${step.thinking}`, step.text,
      ...step.tools.map(call => `${call.name || 'Tool'} · ${call.status}${expanded && call.args ? `\n${call.args}` : ''}${call.progress ? `\n${call.progress}` : ''}${expanded && call.output ? `\n${call.output}` : ''}`)]),
    record.summary].filter(Boolean).join('\n'));
}
