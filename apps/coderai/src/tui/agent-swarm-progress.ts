import { Text, type Component } from '@coderai/pi-tui';
import type { AgentActivityRecord } from './subagent-activity.js';
import { agentActivityText } from './subagent-activity.js';
import { sanitizeTerminalText } from './sanitize.js';

type Outcome = { index: number; status: string; summary?: string };

/** Accept both the current XML result and the older persisted plain format. */
export function swarmOutcomes(output: string): Outcome[] {
  const xml: Outcome[] = [];
  let ordinal = 0;
  for (const match of output.matchAll(/<subagent\b([^>]*)>([\s\S]*?)<\/subagent>/g)) {
    ordinal++;
    const attrs = match[1] ?? '';
    const explicit = Number(/\bindex="([^"]*)"/.exec(attrs)?.[1]);
    const index = Number.isInteger(explicit) && explicit > 0 ? explicit : ordinal;
    const status = /\boutcome="([^"]*)"/.exec(attrs)?.[1];
    if (status && ['completed', 'failed', 'aborted', 'cancelled'].includes(status) && index <= 128) {
      xml.push({ index, status: status === 'aborted' ? 'cancelled' : status, summary: match[2]?.slice(0, 8192) });
    }
  }
  if (xml.length) return xml;
  return output.split(/\n(?=\[agent \d+\]\n)/).flatMap(block => {
    const index = Number(/^\[agent (\d+)\]$/m.exec(block)?.[1]);
    const status = /^status: (completed|failed|aborted|cancelled)$/m.exec(block)?.[1];
    return index > 0 && index <= 128 && status ? [{ index, status: status === 'aborted' ? 'cancelled' : status }] : [];
  });
}

/** A compact progress card with one-based member slots, including work still queued. */
export class AgentSwarmProgressComponent implements Component {
  expanded = false;
  private args: Record<string, unknown> = {};
  private outcomes: Outcome[] = [];
  private phase = 'running';
  constructor(readonly agents: Map<string, AgentActivityRecord>) {}
  setArgs(args: unknown): void { if (args && typeof args === 'object' && !Array.isArray(args)) this.args = args as Record<string, unknown>; }
  complete(output: string, failed: boolean): void { this.outcomes = swarmOutcomes(output); this.phase = failed ? 'failed' : 'ended'; }
  interrupt(): void { this.phase = 'interrupted'; }
  get terminalStatus(): 'completed' | 'failed' | 'interrupted' | undefined {
    if (!this.outcomes.length) return undefined;
    if (this.outcomes.some(outcome => outcome.status === 'failed')) return 'failed';
    if (this.outcomes.some(outcome => outcome.status === 'cancelled')) return 'interrupted';
    return 'completed';
  }
  invalidate(): void {}
  render(width: number): string[] {
    if (width < 1) return [];
    const resumes = this.args['resume_agent_ids'];
    const labels = [
      ...(resumes && typeof resumes === 'object' && !Array.isArray(resumes) ? Object.keys(resumes).map(() => '(resumed)') : []),
      ...(Array.isArray(this.args['items']) ? this.args['items'].map(String) : []),
    ].slice(0, 128);
    const records = [...this.agents.values()];
    const count = Math.min(128, Math.max(labels.length, records.length, ...records.map(agent => agent.swarmIndex ?? 0), ...this.outcomes.map(outcome => outcome.index)));
    const slots: { record?: AgentActivityRecord; status: string; label: string; summary?: string }[] = Array.from({ length: count }, (_, index) => ({ status: this.phase === 'running' ? 'queued' : this.phase === 'failed' ? 'failed' : 'cancelled', label: labels[index] ?? '' }));
    for (const record of records) {
      const index = record.swarmIndex !== undefined && record.swarmIndex > 0 ? record.swarmIndex - 1 : slots.findIndex(slot => !slot.record);
      if (!slots[index]) continue;
      slots[index] = { record, status: record.status, label: labels[index] ?? '' };
    }
    for (const outcome of this.outcomes) {
      const slot = slots[outcome.index - 1]; if (!slot) continue;
      // A background task's precise terminal failure wins over a spawn-success result.
      if (!slot.record || !['failed', 'timed_out', 'killed', 'lost', 'cancelled'].includes(slot.record.status)) slot.status = outcome.status;
      slot.summary = outcome.summary;
    }
    const completed = slots.filter(slot => slot.status === 'completed').length;
    const text = [`Agents: ${completed}/${slots.length} completed`, ...slots.flatMap((slot, index) => {
      const status = ['running', 'backgrounded'].includes(slot.status) ? '●' : slot.status === 'completed' ? '✓' : ['queued', 'suspended'].includes(slot.status) ? '○' : '✗';
      const header = `${status} ${index + 1}. ${slot.record?.name ?? slot.label ?? 'Agent'} · ${slot.status}`;
      const detail = slot.record ? agentActivityText(slot.record, this.expanded) : slot.summary;
      if (!detail) return [header];
      const lines = detail.split('\n');
      return [header, this.expanded ? detail : lines.slice(1).slice(-2).join('\n')];
    })].filter(Boolean).join('\n');
    return new Text(sanitizeTerminalText(text), 1, 0).render(width);
  }
}
