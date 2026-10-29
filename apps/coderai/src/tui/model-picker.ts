import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';

export async function chooseModel(runtime: Runtime, view: TerminalView): Promise<void> {
  const config = await runtime.harness.getConfig();
  const models = Object.entries(config.models ?? {});
  if (!models.length) {
    view.message('No configured models. Add a provider and model with coderai providers add and coderai models add.');
    return;
  }
  const providerIds = [...new Set(models.map(([, model]) => model.provider))].sort();
  const providerName = (id: string) => {
    const name = id === '__kimi_env__' ? config.providers[id]?.type ?? id : id;
    return ({ openai: 'OpenAI', anthropic: 'Anthropic', deepseek: 'DeepSeek', meta: 'Meta', google: 'Google', 'google-genai': 'Google', kimi: 'Kimi' } as Record<string, string>)[name.toLowerCase()] ?? name;
  };
  for (;;) {
    const provider = await view.choose('Choose a provider:', providerIds.map(id => ({ value: id, label: providerName(id) })));
    if (!provider) return;
    const selected = await view.choose(`Choose a model from ${providerName(provider)}:`, [
      ...models.filter(([, model]) => model.provider === provider).map(([alias, model]) => ({ value: alias, label: model.displayName || model.model, description: alias === model.model ? undefined : `Alias: ${alias}` })),
      { value: '\0back', label: 'Back to providers' },
    ]);
    if (!selected) return;
    if (selected === '\0back') continue;
    await runtime.validateModel(selected);
    await runtime.session!.setModel(selected);
    view.message(`Model: ${selected}`);
    return;
  }
}
