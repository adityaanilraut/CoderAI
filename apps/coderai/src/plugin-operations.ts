import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import type { CoderaiHarness, PluginInfo, PluginSummary, CapabilityStatus, ReloadSummary } from '@coderai/sdk';

export type PluginApi = Pick<CoderaiHarness, 'listPlugins' | 'getPluginInfo' | 'installPlugin' | 'setPluginEnabled' | 'setPluginMcpServerEnabled' | 'removePlugin' | 'reloadPlugins' | 'listCapabilities' | 'getCapability' | 'installCapability'>;
export const pluginApplyHint = 'Run /reload or /new to apply changes to this conversation.';
export function pluginListText(plugins: readonly PluginSummary[]): string {
  return plugins.map(p => `${p.displayName} (${p.id})${p.version ? ` v${p.version}` : ''} · ${p.enabled ? 'enabled' : 'disabled'} · ${p.state} · ${p.skillCount} skills · ${p.commandCount} commands · ${p.hookCount} hooks · ${p.enabledMcpServerCount}/${p.mcpServerCount} MCP servers${p.hasErrors ? ' · needs attention' : ''}`).join('\n') || 'No plugins installed.';
}
export function pluginInfoText(p: PluginInfo): string {
  return [pluginListText([p]), `Source: ${p.originalSource ?? p.source}`, `Root: ${p.root}`, `Installed: ${p.installedAt}`,
    p.manifest?.description ?? '', `MCP SERVERS\n${p.mcpServers.map(s => `${s.name} · ${s.enabled ? 'enabled' : 'disabled'} · ${s.transport} · runtime ${s.runtimeName}`).join('\n') || 'None'}`,
    `DIAGNOSTICS\n${p.diagnostics.map(d => `${d.message}`).join('\n') || 'None'}`].filter(Boolean).join('\n\n');
}
export function capabilityText(c: CapabilityStatus): string {
  return [`${c.displayName} (${c.id}) · ${c.state}${c.version ? ` · ${c.version}` : ''}`, c.description,
    ...c.steps.map(step => `${step.id}: ${step.state}${step.detail ? ` — ${step.detail}` : ''}`),
    c.install.running ? `Installing${c.install.step ? `: ${c.install.step}` : ''}${c.install.percent !== undefined ? ` (${c.install.percent}%)` : ''}` : '',
    c.install.error ? `Install failed: ${c.install.error}` : '', c.install.note ?? ''].filter(Boolean).join('\n');
}
export function reloadText(result: ReloadSummary): string {
  return [`Plugins reloaded: ${result.added.length} added, ${result.removed.length} removed.`,
    ...result.errors.map(error => `${error.id}: ${error.message}`)].join('\n');
}
/** One mutation path shared by the terminal panel and CLI. */
export async function pluginAction(api: PluginApi, action: string, args: readonly string[]): Promise<string> {
  if (action === 'list' && !args.length) return pluginListText(await api.listPlugins());
  if (action === 'reload' && !args.length) return reloadText(await api.reloadPlugins());
  if (action === 'info' && args.length === 1) return pluginInfoText(await api.getPluginInfo(args[0]!));
  if (action === 'install' && args.length) { const p = await api.installPlugin(args.join(' ')); return `Plugin installed: ${p.displayName} (${p.id})${p.hasErrors ? ' with diagnostics; inspect plugin info.' : '.'}`; }
  if (['enable', 'disable', 'remove'].includes(action) && args.length === 1) {
    const id = args[0]!;
    if (action === 'remove') await api.removePlugin(id); else await api.setPluginEnabled(id, action === 'enable');
    return `Plugin ${id} ${action === 'remove' ? 'removed' : action === 'enable' ? 'enabled' : 'disabled'}.`;
  }
  if (action === 'mcp' && args.length === 3 && ['enable', 'disable'].includes(args[0]!)) {
    await api.setPluginMcpServerEnabled(args[1]!, args[2]!, args[0] === 'enable');
    return `MCP server ${args[2]} for ${args[1]} ${args[0] === 'enable' ? 'enabled' : 'disabled'}.`;
  }
  if (action === 'capabilities') {
    if (!args.length || args.length === 1 && args[0] === 'list') return (await api.listCapabilities()).map(capabilityText).join('\n\n') || 'No capabilities available.';
    if (args.length === 2 && args[0] === 'info') return capabilityText(await api.getCapability(args[1]!));
    if (args.length === 2 && args[0] === 'install') {
      const current = await api.getCapability(args[1]!);
      return capabilityText(current.install.running ? current : await api.installCapability(args[1]!));
    }
  }
  throw new Error('Usage: plugins list|info ID|install SOURCE|enable ID|disable ID|remove ID|reload|mcp enable|disable ID SERVER|capabilities [list|info ID|install ID]');
}

export function resolvePluginSource(source: string, workDir: string): string {
  const value = source.trim();
  if (/^https?:\/\//.test(value)) return value;
  if (value === '~') return homedir();
  if (value.startsWith('~/')) return join(homedir(), value.slice(2));
  return isAbsolute(value) ? value : resolve(workDir, value);
}
/** The CLI must keep its engine alive while the background installer settles. */
export async function installCapabilityAndWait(api: PluginApi, id: string, options: {
  signal?: AbortSignal; timeoutMs?: number; onProgress?: (status: CapabilityStatus) => void;
} = {}): Promise<CapabilityStatus> {
  options.signal?.throwIfAborted();
  let status = await api.getCapability(id);
  options.signal?.throwIfAborted();
  if (!status.install.running) status = await api.installCapability(id);
  const deadline = Date.now() + (options.timeoutMs ?? 180_000);
  while (status.install.running) {
    options.signal?.throwIfAborted(); options.onProgress?.(status);
    if (Date.now() >= deadline) throw new Error(`Capability ${id}: installation has not settled; inspect its readiness before retrying.`);
    await delay(700, undefined, { signal: options.signal });
    status = await api.getCapability(id);
  }
  if (status.install.error) throw new Error(`Capability ${id}: ${status.install.error}`);
  return status;
}
