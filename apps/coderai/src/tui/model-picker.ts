import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { setup } from '../setup.js';

export async function configureModels(runtime: Runtime, view: TerminalView): Promise<void> {
  try { await view.suspend(() => setup(runtime)); }
  catch (error) { if (!(error instanceof Error) || error.name !== 'AbortError') throw error; }
  await runtime.session?.reloadSession();
}

export async function chooseModel(runtime: Runtime, view: TerminalView): Promise<void> {
  for (;;) {
    const config = await runtime.harness.getConfig();
    const models = Object.entries(config.models ?? {});
    const providerIds = [...new Set([...Object.keys(config.providers ?? {}), ...models.map(([, model]) => model.provider)])].sort();
    const providerName = (id: string) => {
      const name = id === '__kimi_env__' ? config.providers[id]?.type ?? id : id;
      return ({ openai: 'OpenAI', anthropic: 'Anthropic', deepseek: 'DeepSeek', meta: 'Meta', google: 'Google', 'google-genai': 'Google', kimi: 'Kimi' } as Record<string, string>)[name.toLowerCase()] ?? name;
    };
    const provider = await view.chooseSearchable('Choose a provider:', [
      ...providerIds.map(id => ({ value: id, label: providerName(id), description: `${models.filter(([, model]) => model.provider === id).length} configured models` })),
      { value: '\0setup', label: 'Add provider or model…', description: 'Open guided setup, then return here', pinned: true },
    ]);
    if (!provider) return;
    if (provider === '\0setup') { await configureModels(runtime, view); continue; }
    const current = (await runtime.session!.getStatus()).model;
    const selected = await view.chooseSearchable(`Choose a model from ${providerName(provider)}:`, [
      ...models.filter(([, model]) => model.provider === provider).map(([alias, model]) => ({ value: alias, label: `${alias === current ? '[current] ' : ''}${model.displayName || model.model}`, description: [alias === current ? 'Current' : '', alias === model.model ? '' : `Alias: ${alias}`, model.maxContextSize ? `Context: ${model.maxContextSize.toLocaleString()}` : '', model.capabilities?.includes('image_in') ? 'Images' : '', model.capabilities?.includes('thinking') ? 'Thinking' : '', model.capabilities?.includes('video_in') ? 'Video' : '', model.capabilities?.includes('audio_in') ? 'Audio' : '', model.capabilities?.includes('dynamically_loaded_tools') ? 'Tool Select' : ''].filter(Boolean).join(' · ') })),
      { value: '\0setup', label: 'Add provider or model…', description: 'Open guided setup', pinned: true },
      { value: '\0back', label: 'Back to providers', pinned: true },
    ]);
    if (!selected) return;
    if (selected === '\0back') continue;
    if (selected === '\0setup') { await configureModels(runtime, view); continue; }
    await runtime.validateModel(selected);
    await runtime.session!.setModel(selected);
    const model = config.models?.[selected];
    view.message(`Model: ${model?.displayName || model?.model || selected}`);
    return;
  }
}
