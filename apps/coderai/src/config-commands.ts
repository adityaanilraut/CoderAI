import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import type { ProviderType } from '@coderai/sdk';
import type { Runtime } from './runtime.js';

const execFileAsync = promisify(execFile);
const providerTypes: readonly string[] = ['kimi', 'openai', 'openai_responses', 'anthropic', 'google-genai', 'vertexai'];

function validName(name: string): void {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(name) || name.startsWith('__')) throw new Error(`Invalid configuration name: ${name}`);
}

/** Diagnostics never print provider credentials or contact a model service. */
export async function doctor(runtime: Runtime, output: (text: string) => void = text => { process.stdout.write(text); }, failure: (text: string) => void = text => { process.stderr.write(text); }): Promise<number> {
  let failures = 0;
  output(`CoderAI home: ${runtime.harness.homeDir}\nConfiguration: ${runtime.harness.configPath}\nRuntime: Bun ${process.versions['bun'] ?? '(unknown)'}\nProvider connectivity: not tested (local checks only)\n`);
  try { await runtime.validateModel(); output('Model configuration: OK\n'); }
  catch (error) { failures++; failure(`Model configuration: ${error instanceof Error ? error.message : String(error)}\n`); }
  try { await execFileAsync('rg', ['--version'], { timeout: 5000 }); output('File search (ripgrep): OK\n'); }
  catch { failures++; failure('File search: ripgrep (rg) is unavailable on PATH.\n'); }
  const config = await runtime.harness.getConfig();
  const search = config.services?.moonshotSearch;
  const managed = Object.values(config.providers).some(provider => provider.type === 'kimi' && provider.oauth);
  output(`Web search: ${search?.baseUrl?.trim() ? 'configured (custom endpoint)' : managed ? 'configured (managed OAuth)' : 'not configured; use /setup or CODERAI_SEARCH_BASE_URL / CODERAI_SEARCH_API_KEY'}\n`);
  return failures ? 1 : 0;
}

export async function listProviders(runtime: Runtime): Promise<void> {
  const config = await runtime.harness.getConfig();
  const entries = Object.entries(config.providers);
  process.stdout.write(entries.length ? entries.map(([id, provider]) => `${id}\t${provider.type}\t${provider.apiKeyEnv ? `key: ${provider.apiKeyEnv}` : provider.apiKey ? 'key: configured' : 'key: environment/default'}\n`).join('') : 'No providers configured.\n');
}

export async function setProvider(runtime: Runtime, id: string, type: string, baseUrl: string, apiKeyEnv: string, apiKey?: string, operation: 'add' | 'update' = 'add'): Promise<void> {
  validName(id);
  const existing = await runtime.harness.getConfig();
  if (operation === 'add' && existing.providers[id]) throw new Error(`Provider ${id} already exists; use providers update or /setup to change it.`);
  if (operation === 'update' && !existing.providers[id]) throw new Error(`Unknown provider: ${id}`);
  if (operation === 'update' && (existing.providers[id]?.oauth || existing.providers[id]?.source)) throw new Error('This provider is managed by a registry or OAuth. Reconnect it through its source instead.');
  if (!providerTypes.includes(type)) throw new Error(`Unsupported provider type: ${type}`);
  if (apiKey === undefined && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(apiKeyEnv)) throw new Error('The API key environment variable name is invalid.');
  if (apiKey !== undefined && !apiKey.trim()) throw new Error('API key cannot be empty.');
  let url: URL;
  try { url = new URL(baseUrl); } catch { throw new Error('Provider base URL must be an absolute HTTP or HTTPS URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Provider base URL must be HTTP(S) without embedded credentials.');
  const credentials = operation === 'update'
    ? { apiKey: apiKey ?? '', apiKeyEnv: apiKey === undefined ? apiKeyEnv : '' }
    : apiKey === undefined ? { apiKeyEnv } : { apiKey };
  await runtime.harness.setConfig({ providers: { [id]: { type: type as ProviderType, baseUrl, ...credentials } } });
  process.stdout.write(apiKey === undefined ? `Provider ${id} saved. Set ${apiKeyEnv} in your environment.\n` : `Provider ${id} saved with an API key.\n`);
}

export async function removeProvider(runtime: Runtime, id: string): Promise<void> {
  validName(id);
  const config = await runtime.harness.getConfig();
  if (!config.providers[id]) throw new Error(`Unknown provider: ${id}`);
  await runtime.harness.removeProvider(id);
  process.stdout.write(`Provider ${id} and its associated model aliases removed.\n`);
}

export async function listModels(runtime: Runtime): Promise<void> {
  const config = await runtime.harness.getConfig();
  const entries = Object.entries(config.models ?? {});
  process.stdout.write(entries.length ? entries.map(([id, model]) => `${id}\t${model.provider}\t${model.model}${id === config.defaultModel ? '\tdefault' : ''}\n`).join('') : 'No model aliases configured.\n');
}

export async function setModel(runtime: Runtime, alias: string, provider: string, model: string, context: string, capabilities: string, operation: 'add' | 'update' = 'add'): Promise<void> {
  if (operation === 'add') validName(alias);
  if (alias.startsWith('__') || provider.startsWith('__')) throw new Error('Environment model overrides cannot be edited.');
  const config = await runtime.harness.getConfig();
  if (operation === 'add' && config.models?.[alias]) throw new Error(`Model alias ${alias} already exists; use models update or /setup to change it.`);
  if (operation === 'update' && !config.models?.[alias]) throw new Error(`Unknown model alias: ${alias}`);
  if (!config.providers[provider]) throw new Error(`Unknown provider: ${provider}`);
  const size = Number(context);
  if (!Number.isSafeInteger(size) || size <= 0) throw new Error('Context size must be a positive integer.');
  if (!model.trim()) throw new Error('Model identifier cannot be empty.');
  const caps = capabilities ? capabilities.split(',').map(value => value.trim()).filter(Boolean) : [];
  const allowed = ['image_in', 'video_in', 'audio_in', 'dynamically_loaded_tools', 'thinking', ...(operation === 'update' ? config.models?.[alias]?.capabilities ?? [] : [])];
  if (caps.some(cap => !allowed.includes(cap))) throw new Error('Capabilities must be a comma-separated list of image_in, video_in, audio_in, dynamically_loaded_tools, or thinking.');
  await runtime.harness.setConfig({ models: { [alias]: { provider, model, maxContextSize: size, capabilities: caps } } });
  process.stdout.write(`Model ${alias} saved.\n`);
}

export async function importProviders(runtime: Runtime, url: string, keyEnv?: string): Promise<void> {
  const endpoint = new URL(url);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password) throw new Error('Registry URL must be HTTP(S) without embedded credentials.');
  if (keyEnv && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyEnv)) throw new Error('Invalid API key environment variable name.');
  const apiKey = keyEnv ? process.env[keyEnv]?.trim() : undefined;
  if (keyEnv && !apiKey) throw new Error(`Set ${keyEnv} before importing this registry.`);
  const result = await runtime.harness.importCustomRegistry({ url, apiKey, setDefaultWhenUnset: false });
  process.stdout.write(`Imported ${result.providers.length} provider(s), ${result.modelsImported} model(s).\n`);
}

export async function refreshProviders(runtime: Runtime): Promise<void> {
  const result = await runtime.harness.refreshProviderModels();
  for (const item of result.changed) process.stdout.write(`${item.provider_id}: +${item.added} / -${item.removed} models\n`);
  if (!result.changed.length && !result.failed.length) process.stdout.write('Provider models are up to date.\n');
  if (result.failed.length) throw new Error(`Refresh failed for: ${result.failed.map(item => item.provider).join(', ')}. Check the registry connection and credentials.`);
}

export async function setDefaultModel(runtime: Runtime, alias: string, secondary = false): Promise<void> {
  if (alias.startsWith('__')) throw new Error('Environment model overrides cannot be saved as defaults.');
  const config = await runtime.harness.getConfig();
  if (!config.models?.[alias]) throw new Error(`Unknown model alias: ${alias}`);
  await runtime.harness.setConfig(secondary ? { secondaryModel: { defaultModel: alias } } : { defaultModel: alias });
  process.stdout.write(`${secondary ? 'Secondary' : 'Default'} model: ${alias}\n${!secondary && process.env['CODERAI_MODEL_NAME'] ? 'CODERAI_MODEL_NAME currently overrides the saved default.\n' : ''}`);
}

export async function setDefaultPermission(runtime: Runtime, mode: string): Promise<void> {
  if (mode !== 'manual' && mode !== 'yolo' && mode !== 'auto') throw new Error('Permission mode must be manual, yolo, or auto.');
  await runtime.harness.setConfig({ defaultPermissionMode: mode });
  process.stdout.write(`Default permission mode: ${mode}\n`);
}
