import type { Event, ToolUpdate } from '@coderai/sdk';
import { browserUrl } from './browser.js';

type Of<T extends Event['type']> = Extract<Event, { type: T }>;

export function formatHookResult(event: Of<'hook.result'>): string {
  return `${event.hookEvent} hook${event.blocked === true ? ' blocked' : ''}\n\n${event.content.trim() || '(empty)'}`;
}

export function formatInterruption(event: Of<'turn.step.interrupted'>): string | undefined {
  if (event.reason === 'error') return undefined; // The error event carries the diagnosis.
  if (!event.reason || event.reason === 'aborted') return event.message || 'Interrupted by user';
  return event.reason === 'max_steps' ? 'reached per-turn step limit (max_steps)' : `step interrupted (${event.reason})`;
}

export function formatCron(event: Of<'cron.fired'>): string {
  const data = event.origin;
  const detail = [data.cron, data.jobId && `job ${data.jobId}`, data.recurring === false ? 'one-shot' : data.recurring === true ? 'recurring' : '',
    data.coalescedCount && data.coalescedCount > 1 ? `${data.coalescedCount} fires coalesced` : '',
    data.stale ? 'final delivery' : ''].filter(Boolean).join(' | ');
  return `Scheduled reminder fired${detail ? `\n${detail}` : ''}\n${event.prompt}`;
}

export function parseMcpAuthorization(update: ToolUpdate): { serverName: string; authorizationUrl: string } | undefined {
  if (update.kind !== 'custom' || update.customKind !== 'mcp.oauth.authorization_url') return;
  const data = update.customData;
  if (!data || typeof data !== 'object' || Array.isArray(data)) return;
  const { serverName, authorizationUrl } = data as Record<string, unknown>;
  if (typeof serverName !== 'string' || !serverName || typeof authorizationUrl !== 'string' || !authorizationUrl) return;
  try { browserUrl(authorizationUrl); return { serverName, authorizationUrl }; } catch { return; }
}

/** One browser attempt per call/URL, even when the server repeats a progress update. */
export class McpAuthorizationOpener {
  private readonly seen = new Set<string>();
  constructor(private readonly show: (text: string) => void, private readonly open?: (url: string) => Promise<boolean>) {}
  handle(event: Of<'tool.progress'>): void {
    const data = parseMcpAuthorization(event.update);
    if (!data) return;
    const key = JSON.stringify([event.agentId, event.toolCallId, data.authorizationUrl]);
    if (this.seen.has(key)) return;
    this.seen.add(key);
    this.show(`Authorize MCP server ${data.serverName}:\n${data.authorizationUrl}`);
    if (this.open) void this.open(data.authorizationUrl).then(opened => {
      if (!opened) this.show('The browser could not open. Use the authorization URL above.');
    }).catch(() => this.show('The browser could not open. Use the authorization URL above.'));
  }
  clear(): void { this.seen.clear(); }
}

export function eventNotice(event: Event): string | undefined {
  switch (event.type) {
    case 'hook.result': return formatHookResult(event);
    case 'cron.fired': return formatCron(event);
    case 'turn.step.interrupted': return formatInterruption(event);
    case 'plugin_command.activated': return `Plugin command activated: /${event.pluginId}:${event.commandName}${event.commandArgs ? ` ${event.commandArgs}` : ''}`;
    case 'tool.list.updated': return `Tools updated: ${event.serverName} · ${event.reason}`;
    case 'warning': return `Warning: ${event.message}`;
  }
}

/** Notices occupy their own transcript position; subsequent deltas need a new text block. */
export function renderEventNotice(view: { beginAssistant(newTurn?: boolean): void; message(text: string): void }, event: Event): boolean {
  const notice = eventNotice(event);
  if (!notice) return false;
  view.beginAssistant(false);
  view.message(notice);
  return true;
}
