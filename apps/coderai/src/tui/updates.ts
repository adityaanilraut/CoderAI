import { Text, truncateToWidth, type Component } from '@coderai/pi-tui';
import type { Event } from '@coderai/sdk';
import { sanitizeTerminalText } from './sanitize.js';

/** Match upstream delivery semantics: only commit an update after a successful displayed result. */
export class UpdatesPanel implements Component {
  private readonly pending = new Map<string, { title: string; message: string }>();
  private readonly displayed = new Set<string>();
  isDisplayed(agentId: string, toolCallId: string): boolean { return this.displayed.has(JSON.stringify([agentId, toolCallId])); }
  private readonly updates: { title: string; message: string }[] = [];
  constructor(private readonly rows: () => number) {}
  clear(): void { this.pending.clear(); this.displayed.clear(); this.updates.length = 0; }
  handle(event: Event): boolean {
    if (event.type === 'turn.started' && event.agentId === 'main') this.clear();
    if (event.type === 'turn.ended') {
      const prefix = JSON.stringify([event.agentId]).slice(0, -1) + ',';
      for (const key of this.pending.keys()) if (key.startsWith(prefix)) this.pending.delete(key);
    }
    if (event.type !== 'tool.call.started' && event.type !== 'tool.result') return false;
    const key = JSON.stringify([event.agentId, event.turnId, event.toolCallId]);
    if (event.type === 'tool.call.started') {
      this.displayed.delete(JSON.stringify([event.agentId, event.toolCallId]));
      if (event.name !== 'NotifyUser' || !event.args || typeof event.args !== 'object') return false;
      const args = event.args as Record<string, unknown>;
      if (typeof args['message'] === 'string' && args['message'].trim()) this.pending.set(key, { title: typeof args['title'] === 'string' ? args['title'] : 'Update', message: args['message'] });
      return false;
    }
    const update = this.pending.get(key); this.pending.delete(key);
    if (!update || event.isError || event.synthetic || event.output !== 'Update shown to the user.') return false;
    this.displayed.add(JSON.stringify([event.agentId, event.toolCallId]));
    this.updates.push(update); if (this.updates.length > 5) this.updates.shift(); return true;
  }
  invalidate(): void {}
  render(width: number): string[] {
    if (!this.updates.length || width < 3) return [];
    const update = this.updates.at(-1)!;
    const maxRows = Math.max(1, Math.min(7, Math.floor(this.rows() / 4)));
    return new Text(sanitizeTerminalText(`Updates · ${update.title}\n${update.message}`), 1, 0).render(width).slice(0, maxRows).map(line => truncateToWidth(line, width, '…'));
  }
}
