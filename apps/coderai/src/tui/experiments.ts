import { Key, matchesKey } from '@coderai/pi-tui';
import type { ExperimentalFeatureState } from '@coderai/sdk';
import { ChoiceDialog } from './dialogs.js';
import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';

export class ExperimentsDialog extends ChoiceDialog {
  readonly changes = new Map<string, boolean>();
  constructor(private readonly features: readonly ExperimentalFeatureState[], rows: () => number, private readonly done: (answer: string) => void, private readonly rerender: () => void) {
    super('Experimental features', [], rows, done, rerender, { searchable: true, selectHint: '↑/↓ choose · Space toggle · Enter apply · Esc cancel' }); this.updateItems();
  }
  private updateItems(): void {
    this.setItems(this.features.map(feature => ({ value: feature.id,
      label: `${this.changes.get(feature.id) ?? feature.enabled ? '[on]' : '[off]'} ${feature.title}`,
      description: `${feature.id}${feature.id.includes('-') ? ` (${feature.id.replaceAll('-', '_')})` : ''} · ${feature.source === 'env' || feature.source === 'master-env' ? `locked by ${feature.source === 'env' ? feature.env : 'KIMI_CODE_EXPERIMENTAL_FLAG'}` : feature.source} · ${feature.description}`,
    })));
    this.setNotice(`${this.changes.size} change(s) · Space toggles · Enter saves and reloads · Esc cancels`);
  }
  override handleInput(data: string): void {
    if (matchesKey(data, Key.enter)) { this.done('apply'); return; }
    if (matchesKey(data, Key.space)) {
      const feature = this.features.find(feature => feature.id === this.selection?.value);
      if (feature && feature.source !== 'env' && feature.source !== 'master-env') {
        const enabled = !(this.changes.get(feature.id) ?? feature.enabled);
        if (enabled === feature.enabled) this.changes.delete(feature.id); else this.changes.set(feature.id, enabled);
        this.updateItems(); this.rerender();
      }
      return;
    }
    super.handleInput(data);
  }
}
export async function showExperiments(runtime: Runtime, view: TerminalView): Promise<void> {
  const features = await runtime.harness.getExperimentalFeatures();
  if (!features.length) { view.message('No experimental features available.'); return; }
  let dialog!: ExperimentsDialog;
  const result = await view.showDialog(finish => {
    dialog = new ExperimentsDialog(features, () => view.tui.terminal.rows, finish, () => view.tui.requestRender()); return dialog;
  }, '↑/↓ navigate · Space toggle · Enter apply · Esc cancel');
  if (result !== 'apply') return;
  if (!dialog.changes.size) { view.message('No experimental feature changes.'); return; }
  await runtime.harness.setConfig({ experimental: Object.fromEntries(dialog.changes) });
  await runtime.harness.getExperimentalFeatures();
  if (runtime.session && [...dialog.changes.keys()].some(id => id !== 'notify_user')) {
    try { await runtime.session.reloadSession(); view.onSessionReloaded?.(); }
    catch (error) { view.notice('Experimental preferences were saved, but the session could not reload. Finish active work and use /reload to retry.'); throw error; }
  }
  view.message('Experimental features saved. Session configuration refreshed.');
  if (dialog.changes.get('notify_user')) view.notice('Start a new session to use Updates if this session was created with the feature disabled.');
  if (dialog.changes.has('tower')) view.notice('Tower tools take effect after restarting CoderAI.');
}
