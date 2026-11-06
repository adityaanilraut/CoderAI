import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { capabilityText, resolvePluginSource, pluginAction, pluginApplyHint, pluginInfoText } from '../plugin-operations.js';
import { ChoiceDialog } from './dialogs.js';

async function installCapability(runtime: Runtime, view: TerminalView, id: string): Promise<void> {
  const api = runtime.harness;
  let status = await api.getCapability(id);
  if (!status.supported) { await view.showDocument(status.displayName, capabilityText(status)); return; }
  if (!status.install.running) status = await api.installCapability(id);
  if (!status.install.running) { await view.showDocument(status.displayName, capabilityText(status)); return; }
  let timer: ReturnType<typeof setTimeout> | undefined;
  let closed = false;
  try {
    await view.showDialog(finish => {
      const panel = new ChoiceDialog(`${status.displayName}\n${capabilityText(status)}`, [{ value: 'close', label: 'Close', description: 'Installation continues in the background; reopen to inspect progress' }], () => view.tui.terminal.rows, finish, () => view.tui.requestRender());
      const poll = async () => {
        try {
          status = await api.getCapability(id);
          if (closed) return;
          panel.setNotice(capabilityText(status)); view.tui.requestRender();
          if (status.install.running) timer = setTimeout(() => void poll(), 700);
        } catch (error) { if (!closed) { panel.setNotice(`Status unavailable: ${String(error)}\nClose and reopen to retry.`); view.tui.requestRender(); } }
      };
      timer = setTimeout(() => void poll(), 700);
      return panel;
    }, 'Capability installation · Esc closes the panel', { onDismiss: () => { closed = true; if (timer) clearTimeout(timer); } });
  } finally { closed = true; if (timer) clearTimeout(timer); }
}

async function confirmInstall(view: TerminalView, source: string): Promise<boolean> {
  return await view.choose(`Install plugin?\n${source}\n\nPlugins can provide hooks and MCP programs that run on this device. Install sources you trust.`, [
    { value: 'cancel', label: 'Cancel installation' }, { value: 'install', label: 'Install plugin' },
  ]) === 'install';
}

export async function showPlugins(runtime: Runtime, view: TerminalView, argument: string): Promise<void> {
  const api = runtime.harness;
  if (argument.trim()) {
    const [action, ...args] = argument.trim().split(/\s+/);
    if (action === 'capabilities' && args[0] === 'install' && args.length === 2) { await installCapability(runtime, view, args[1]!); return; }
    if (action === 'install' && args.length) args.splice(0, args.length, resolvePluginSource(args.join(' '), runtime.session?.workDir ?? runtime.options.cwd));
    if (action === 'install' && !await confirmInstall(view, args[0]!)) { view.message('Plugin installation cancelled.'); return; }
    const text = await pluginAction(api, action!, args);
    if (['info', 'capabilities'].includes(action!)) await view.showDocument('Plugins', text);
    else view.message(`${text}${runtime.session ? `\n${pluginApplyHint}` : ''}`);
    return;
  }
  let selectedPlugin: string | undefined;
  for (;;) {
    view.setStatus('Loading plugins…');
    const plugins = await api.listPlugins();
    const selected = await view.chooseSearchable('Plugins', [
      ...plugins.map(p => ({ value: `plugin:${p.id}`, label: `${p.displayName} · ${p.enabled ? 'enabled' : 'disabled'}${p.hasErrors ? ' · needs attention' : ''}`, description: `${p.id} · ${p.skillCount} skills · ${p.enabledMcpServerCount}/${p.mcpServerCount} MCP servers` })),
      { value: 'install', label: 'Install a plugin', description: plugins.length ? 'Local directory, ZIP URL or GitHub source' : 'No plugins installed. Install a local directory, ZIP URL or GitHub source.' },
      { value: 'capabilities', label: 'Capabilities', description: 'Inspect runtime readiness and install built-in capabilities' },
      { value: 'reload', label: 'Reload plugins' },
    ], selectedPlugin);
    if (!selected) return;
    selectedPlugin = selected;
    try {
      if (selected === 'install') {
        const source = (await view.ask('Plugin source:', 'Enter local path, ZIP URL or GitHub source · Esc cancel')).trim();
        if (source && await confirmInstall(view, source)) view.message(await pluginAction(api, 'install', [resolvePluginSource(source, runtime.session?.workDir ?? runtime.options.cwd)]) + `\n${pluginApplyHint}`);
      } else if (selected === 'reload') view.message(await pluginAction(api, 'reload', []) + `\n${pluginApplyHint}`);
      else if (selected === 'capabilities') {
        const capabilities = await api.listCapabilities();
        const id = await view.chooseSearchable('Capability readiness', capabilities.map(c => ({ value: c.id, label: `${c.displayName} · ${c.state}`, description: c.description })));
        if (!id) continue;
        const c = capabilities.find(c => c.id === id)!;
        const action = await view.choose(`${c.displayName}\n${capabilityText(c)}`, [
          { value: 'info', label: 'Inspect readiness' }, ...(c.supported ? [{ value: 'install', label: c.install.running ? 'Follow installation' : 'Install or repair capability' }] : []),
        ]);
        if (action === 'info') await view.showDocument(c.displayName, capabilityText(await api.getCapability(id)));
        if (action === 'install') await installCapability(runtime, view, id);
      } else {
        const id = selected.slice(7);
        for (;;) {
          const p = await api.getPluginInfo(id);
          const action = await view.choose(`${p.displayName} (${p.id})`, [
            { value: 'info', label: 'Plugin information' },
            { value: p.enabled ? 'disable' : 'enable', label: p.enabled ? 'Disable plugin' : 'Enable plugin' },
            ...(p.mcpServers.length ? [{ value: 'mcp', label: 'Manage MCP servers', description: `${p.enabledMcpServerCount}/${p.mcpServerCount} enabled` }] : []),
            { value: 'remove', label: 'Remove plugin' },
          ]);
          if (!action) break;
          if (action === 'info') await view.showDocument(p.displayName, pluginInfoText(p));
          else if (action === 'mcp') {
            let selectedServer: string | undefined;
            for (;;) {
              const info = await api.getPluginInfo(id);
              const server = await view.chooseSearchable('Plugin MCP servers', info.mcpServers.map(s => ({ value: s.name, label: `${s.name} · ${s.enabled ? 'enabled' : 'disabled'}`, description: `${s.transport} · runtime ${s.runtimeName}` })), selectedServer);
              if (!server) break;
              selectedServer = server;
              const config = info.mcpServers.find(s => s.name === server)!;
              view.message(await pluginAction(api, 'mcp', [config.enabled ? 'disable' : 'enable', id, server]) + `\n${pluginApplyHint}`);
            }
          } else if (action === 'remove') {
            if (await view.choose(`Remove ${p.displayName}?`, [{ value: 'cancel', label: 'Keep plugin' }, { value: 'remove', label: 'Remove plugin' }]) !== 'remove') continue;
            view.message(await pluginAction(api, 'remove', [id]) + `\n${pluginApplyHint}`); break;
          } else view.message(await pluginAction(api, action, [id]) + `\n${pluginApplyHint}`);
        }
      }
    } catch (error) { view.error(error); }
  }
}
