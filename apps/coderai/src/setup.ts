import chalk from 'chalk';
import type { Runtime } from './runtime.js';
import { setDefaultModel, setModel, setProvider } from './config-commands.js';
import { discoverProviderModels, type DiscoveredModel } from './setup-models.js';
import { terminalPrompt, SetupCancelled, singleLine, type Prompt, type SetupOption } from './setup-terminal.js';

const presets = [
  { name: 'OpenAI', id: 'openai', type: 'openai', url: 'https://api.openai.com/v1', key: 'OPENAI_API_KEY' },
  { name: 'Anthropic', id: 'anthropic', type: 'anthropic', url: 'https://api.anthropic.com', key: 'ANTHROPIC_API_KEY' },
  { name: 'DeepSeek', id: 'deepseek', type: 'openai', url: 'https://api.deepseek.com', key: 'DEEPSEEK_API_KEY' },
  { name: 'Google Gemini', id: 'google', type: 'google-genai', url: 'https://generativelanguage.googleapis.com', key: 'GOOGLE_API_KEY' },
  { name: 'Meta / OpenAI-compatible', id: 'meta', type: 'openai', url: '', key: 'META_API_KEY' },
  { name: 'Custom provider', id: '', type: '', url: '', key: '' },
];
const protocols = [
  { label: 'OpenAI Chat Completions', description: 'openai · also used by DeepSeek and compatible services', type: 'openai' },
  { label: 'OpenAI Responses', description: 'openai_responses', type: 'openai_responses' },
  { label: 'Anthropic Messages', description: 'anthropic', type: 'anthropic' },
  { label: 'Google Gemini', description: 'google-genai', type: 'google-genai' },
  { label: 'Kimi', description: 'kimi', type: 'kimi' },
  { label: 'Google Vertex AI', description: 'vertexai', type: 'vertexai' },
];
const nameError = (value: string) => /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(value) && !value.startsWith('__') ? '' : 'Use letters, numbers, hyphens, or underscores; start with a letter or number.';
const note = (text: string) => process.stdout.write(`  ${singleLine(text)}\n`);
const failure = (text: string) => process.stderr.write(`  ${chalk.yellow('!')} ${singleLine(text)}\n`);
const visibleEntries = <T>(entries: Record<string, T>) => Object.entries(entries).filter(([id]) => !id.startsWith('__'));
function availableName(base: string, entries: Record<string, unknown>): string {
  const cleaned = base.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^[^A-Za-z0-9]+/, '') || 'model';
  let name = cleaned;
  for (let suffix = 2; entries[name]; suffix++) name = `${cleaned}-${suffix}`;
  return name;
}

async function ask(prompt: Prompt, label: string, fallback = '', validate: (value: string) => string = value => value ? '' : 'This field is required.', secret = false): Promise<string> {
  for (;;) {
    const value = (await prompt(`  ${label}${fallback ? chalk.dim(` [${singleLine(fallback)}]`) : ''}: `, secret)).trim() || fallback;
    const error = validate(value);
    if (!error) return value;
    failure(error);
  }
}
async function menu(prompt: Prompt, title: string, options: (string | SetupOption)[], initial = 0, back = 'Back'): Promise<number> {
  const items = options.map(item => typeof item === 'string' ? { label: item } : item);
  if (prompt.select) return prompt.select(title, items, initial, back);
  process.stdout.write(`\n${title}\n${items.map((item, i) => `  ${i + 1}. ${singleLine(item.label)}${item.description ? ` — ${singleLine(item.description)}` : ''}`).join('\n')}\n  0. ${back}\n`);
  for (;;) {
    const value = (await prompt(`Choose a number [${initial + 1}]: `)).trim() || String(initial + 1);
    if (/^\d+$/.test(value) && Number(value) <= items.length) return Number(value);
    failure('Enter one of the listed numbers.');
  }
}
async function capabilities(prompt: Prompt, initial: string[]): Promise<string> {
  const selected = new Set(initial);
  const options = [
    { label: 'Image input', value: 'image_in' },
    { label: 'Video input', value: 'video_in' },
    { label: 'Thinking / reasoning', value: 'thinking' },
  ];
  for (;;) {
    const choice = await menu(prompt, 'Model capabilities · select to toggle', [
      { label: 'Done', description: selected.size ? [...selected].join(', ') : 'Text only · no optional capabilities' },
      ...options.map(option => ({ label: `${selected.has(option.value) ? '[x]' : '[ ]'} ${option.label}` })),
    ]);
    if (!choice) throw new SetupCancelled();
    if (choice === 1) return [...selected].join(',');
    const value = options[choice - 2]!.value;
    selected.has(value) ? selected.delete(value) : selected.add(value);
  }
}
function contextTokens(value: string): number {
  const match = value.replaceAll(',', '').match(/^(\d+(?:\.\d+)?)\s*([km])?$/i);
  return match ? Number(match[1]) * (match[2]?.toLowerCase() === 'k' ? 1000 : match[2]?.toLowerCase() === 'm' ? 1000000 : 1) : NaN;
}

export async function runSetup(runtime: Runtime, prompt: Prompt, discover = discoverProviderModels): Promise<void> {
  process.stdout.write(`\n  ${chalk.bold.cyan('CoderAI')} ${chalk.bold('Setup')}\n  ${chalk.dim(singleLine(runtime.harness.configPath))}\n`);
  note('Esc goes back · Ctrl+C exits · changes save after each completed step');

  const addModel = async (providerID?: string, forceDefault = false) => {
    const config = await runtime.harness.getConfig();
    const providers = visibleEntries(config.providers);
    if (!providers.length) { note('Add a provider to connect your first model.'); await addProvider(); return; }
    if (!providerID) {
      const choice = await menu(prompt, 'Add model · choose a provider', providers.map(([id, provider]) => ({ label: id, description: provider.type })));
      if (!choice) return;
      providerID = providers[choice - 1]![0];
    }
    const provider = config.providers[providerID]!;
    let model: DiscoveredModel | undefined;
    for (;;) {
      const source = await menu(prompt, `Add model · ${providerID}`, [
        { label: 'Browse models from provider', description: 'Load available model IDs using your configured credentials' },
        { label: 'Enter a model ID manually', description: 'Works offline or when model discovery is unavailable' },
      ]);
      if (!source) return;
      if (source === 2) break;
      note('Loading available models…');
      let models: DiscoveredModel[];
      // Allow Ctrl+C to stop a slow metadata request and still restore terminal state.
      const controller = new AbortController();
      const cancel = () => controller.abort();
      process.once('SIGINT', cancel); process.once('SIGTERM', cancel);
      try { models = await discover(provider, controller.signal); }
      catch (error) {
        if (controller.signal.aborted || (error instanceof Error && error.name === 'AbortError')) throw error;
        failure(error instanceof Error ? error.message : 'Could not load models.');
        continue;
      } finally { process.off('SIGINT', cancel); process.off('SIGTERM', cancel); }
      if (!models.length) { note('No models were returned. Enter a model ID manually or try another provider.'); continue; }
      const choice = await menu(prompt, `Available models · ${providerID}`, [
        ...models.map(item => ({ label: item.id, description: [item.name !== item.id ? item.name : '', item.context ? `${item.context.toLocaleString('en-US')} tokens` : 'Context limit required'].filter(Boolean).join(' · ') })),
        { label: 'Enter a model ID manually' },
      ]);
      if (!choice) continue;
      if (choice <= models.length) model = models[choice - 1];
      break;
    }
    const modelID = model?.id ?? await ask(prompt, 'Model ID from your provider', '', value => value && !/[\x00-\x1f\x7f-\x9f]/.test(value) ? '' : 'Enter a model ID without control characters.');
    const alias = await ask(prompt, 'Model alias', availableName(modelID, config.models ?? {}), value => nameError(value) || (config.models?.[value] ? 'That alias already exists. Choose another name.' : ''));
    note(model?.context ? 'Context limit supplied by the provider; press Enter to keep it.' : 'Enter the context limit from your provider (for example 128000 or 128k).');
    const context = await ask(prompt, 'Context limit in tokens', model?.context ? String(model.context) : '', value => Number.isSafeInteger(contextTokens(value)) && contextTokens(value) > 0 ? '' : 'Enter a positive whole token count, such as 128000, 128k, or 1m.');
    const caps = await capabilities(prompt, model?.capabilities ?? []);
    await setModel(runtime, alias, providerID, modelID, String(contextTokens(context)), caps);
    if (forceDefault) await setDefaultModel(runtime, alias);
    else {
      const makeDefault = await menu(prompt, 'Use this model by default?', [
        { label: 'Yes, use this model', description: alias },
        { label: 'Keep the current default', description: config.defaultModel || 'Leave the default unset' },
      ], config.defaultModel ? 1 : 0);
      if (makeDefault === 1) await setDefaultModel(runtime, alias);
    }
  };

  const addProvider = async () => {
    const choice = await menu(prompt, 'Add provider · choose a service', presets.map(p => ({ label: p.name, description: p.url || 'Bring your own API endpoint' })));
    if (!choice) return;
    const preset = presets[choice - 1]!;
    const config = await runtime.harness.getConfig();
    const id = await ask(prompt, 'Provider ID', preset.id ? availableName(preset.id, config.providers) : '', value => nameError(value) || (config.providers[value] ? 'That provider already exists. Choose another ID or add a model to the existing provider.' : ''));
    let type = preset.type;
    if (!type) {
      const protocol = await menu(prompt, 'API protocol', protocols);
      if (!protocol) return;
      type = protocols[protocol - 1]!.type;
    } else note(`API protocol: ${type} · selected for ${preset.name}`);
    const normalizeURL = (value: string) => value.match(/^\[[^\]]+\]\((https?:\/\/[^)]+)\)$/)?.[1] ?? value;
    const url = normalizeURL(await ask(prompt, 'API base URL', preset.url, value => {
      try { const parsed = new URL(normalizeURL(value)); return ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password && !parsed.hash && !parsed.search ? '' : 'Use an HTTP(S) base URL without credentials, query parameters, or fragments.'; }
      catch { return 'Enter a complete API base URL, starting with https:// or http://.'; }
    }));
    const auth = await menu(prompt, 'API key · choose how to save it', [
      { label: 'Enter an API key', description: 'Input hidden · stored unencrypted in your local configuration' },
      { label: 'Use an environment variable', description: 'Store only the variable name in your configuration' },
    ]);
    if (!auth) return;
    if (auth === 1) {
      const key = await ask(prompt, 'API key (input hidden)', '', value => value && !/[\x00-\x20\x7f]/.test(value) ? '' : 'Enter a nonempty API key without whitespace.', true);
      await setProvider(runtime, id, type, url, '', key);
    } else {
      const keyEnv = await ask(prompt, 'Environment variable name', preset.key || `${id.toUpperCase().replaceAll('-', '_')}_API_KEY`, value => /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? '' : 'Enter a variable name such as DEEPSEEK_API_KEY, rather than the key itself.');
      await setProvider(runtime, id, type, url, keyEnv);
      if (!process.env[keyEnv]?.trim()) note(`${keyEnv} is not set in this terminal. You can enter a model manually now and set the variable before chatting.`);
    }
    const next = await menu(prompt, `Provider ${id} is ready · next step`, [
      { label: 'Select and add a model now', description: 'Make this provider available in the default-model picker' },
      { label: 'Return to setup', description: 'Keep the provider and add a model later' },
    ]);
    if (next === 1) await addModel(id);
  };

  for (;;) {
    // A configuration read failure cannot be corrected by reprinting the same menu.
    const config = await runtime.harness.getConfig();
    try {
      const providers = visibleEntries(config.providers);
      const models = visibleEntries(config.models ?? {});
      process.stdout.write(`\n  ${chalk.dim('─'.repeat(Math.max(1, Math.min(64, (process.stdout.columns || 80) - 4))))}\n`);
      note(`${providers.length} provider${providers.length === 1 ? '' : 's'} · ${models.length} model${models.length === 1 ? '' : 's'} · Default: ${config.defaultModel || 'not set'}`);
      if (process.env['CODERAI_MODEL_NAME'] || process.env['KIMI_MODEL_NAME']) note('A model environment override is active; it takes precedence over the saved default.');
      const action = await menu(prompt, 'Setup', [
        { label: 'Add a provider', description: 'Connect OpenAI, Anthropic, DeepSeek, Google, or a custom service' },
        { label: 'Add a model', description: 'Browse provider models or enter a model ID' },
        { label: 'Choose default model', description: config.defaultModel ? `Current: ${config.defaultModel}` : 'Choose the model used for new conversations' },
        { label: 'View configuration', description: 'Review providers, credentials status, and model aliases' },
      ], 0, 'Finish setup');
      if (!action) {
        note(config.defaultModel && config.models?.[config.defaultModel] ? 'Setup saved. Run coderai to start a conversation.' : 'Setup closed. Run coderai --setup to select a default model before chatting.');
        return;
      }
      if (action === 1) await addProvider();
      else if (action === 2) await addModel();
      else if (action === 3) {
        if (!providers.length) { note('Connect a provider, then select its model.'); await addProvider(); continue; }
        const missing = providers.filter(([id]) => !models.some(([, model]) => model.provider === id));
        const options = [
          ...models.map(([alias, model]) => ({ label: `${alias}${alias === config.defaultModel ? '  ✓ current' : ''}`, description: `${model.provider} / ${model.model}` })),
          ...missing.map(([id]) => ({ label: `${id} · add a model`, description: 'Provider connected · no models configured yet' })),
          { label: 'Add another model', description: 'Choose from any configured provider' },
        ];
        const choice = await menu(prompt, 'Default model', options, Math.max(0, models.findIndex(([alias]) => alias === config.defaultModel)));
        if (choice && choice <= models.length) await setDefaultModel(runtime, models[choice - 1]![0]);
        else if (choice) await addModel(missing[choice - models.length - 1]?.[0], true);
      } else {
        note('Providers');
        if (!providers.length) note('  No providers configured.');
        for (const [id, provider] of providers) {
          const keyStatus = provider.apiKey ? 'API key saved' : provider.apiKeyEnv ? `${provider.apiKeyEnv} (${process.env[provider.apiKeyEnv]?.trim() ? 'set' : 'not set'})` : 'environment / default credentials';
          note(`  ${id} · ${provider.type} · ${keyStatus}`);
        }
        note('Models');
        if (!models.length) note('  No models configured. Add one to start chatting.');
        for (const [alias, model] of models) note(`  ${alias} · ${model.provider} / ${model.model} · ${model.maxContextSize.toLocaleString('en-US')} tokens${alias === config.defaultModel ? ' · DEFAULT' : ''}`);
        for (const [id] of providers.filter(([id]) => !models.some(([, model]) => model.provider === id))) note(`  ${id} has no models yet. Choose Add a model to finish connecting it.`);
      }
    } catch (error) {
      if (error instanceof SetupCancelled) { note('Step cancelled. Completed changes are saved.'); continue; }
      if (error instanceof Error && (error.name === 'AbortError' || error.name === 'ExitPromptError')) throw error;
      failure(error instanceof Error ? error.message : String(error));
    }
  }
}

export async function setup(runtime: Runtime): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('--setup requires an interactive terminal.');
  const previousUmask = process.umask(0o077);
  try { await runSetup(runtime, terminalPrompt()); }
  finally { process.stdout.write('\x1b[?25h'); process.umask(previousUmask); }
}
