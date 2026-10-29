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
export async function doctor(runtime: Runtime): Promise<number> {
  let failures = 0;
  process.stdout.write(`CoderAI home: ${runtime.harness.homeDir}\nConfiguration: ${runtime.harness.configPath}\nNode: ${process.version}\n`);
  try { await runtime.validateModel(); process.stdout.write('Model configuration: OK\n'); }
  catch (error) { failures++; process.stderr.write(`Model configuration: ${error instanceof Error ? error.message : String(error)}\n`); }
  try { await execFileAsync('rg', ['--version'], { timeout: 5000 }); process.stdout.write('File search (ripgrep): OK\n'); }
  catch { failures++; process.stderr.write('File search: ripgrep (rg) is unavailable on PATH.\n'); }
  return failures ? 1 : 0;
}

export async function listProviders(runtime: Runtime): Promise<void> {
  const config = await runtime.harness.getConfig();
  const entries = Object.entries(config.providers);
  process.stdout.write(entries.length ? entries.map(([id, provider]) => `${id}\t${provider.type}\t${provider.apiKeyEnv ? `key: ${provider.apiKeyEnv}` : provider.apiKey ? 'key: configured' : 'key: environment/default'}\n`).join('') : 'No providers configured.\n');
}

export async function setProvider(runtime: Runtime, id: string, type: string, baseUrl: string, apiKeyEnv: string, apiKey?: string): Promise<void> {
  validName(id);
  const existing = await runtime.harness.getConfig();
  if (existing.providers[id]) throw new Error(`Provider ${id} already exists; remove it first to replace it.`);
  if (!providerTypes.includes(type)) throw new Error(`Unsupported provider type: ${type}`);
  if (apiKey === undefined && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(apiKeyEnv)) throw new Error('The API key environment variable name is invalid.');
  if (apiKey !== undefined && !apiKey.trim()) throw new Error('API key cannot be empty.');
  let url: URL;
  try { url = new URL(baseUrl); } catch { throw new Error('Provider base URL must be an absolute HTTP or HTTPS URL.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('Provider base URL must be HTTP(S) without embedded credentials.');
  await runtime.harness.setConfig({ providers: { [id]: { type: type as ProviderType, baseUrl, ...(apiKey === undefined ? { apiKeyEnv } : { apiKey }) } } });
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

export async function setModel(runtime: Runtime, alias: string, provider: string, model: string, context: string, capabilities: string): Promise<void> {
  validName(alias); validName(provider);
  const config = await runtime.harness.getConfig();
  if (config.models?.[alias]) throw new Error(`Model alias ${alias} already exists; edit the configuration file to change it.`);
  if (!config.providers[provider]) throw new Error(`Unknown provider: ${provider}`);
  const size = Number(context);
  if (!Number.isSafeInteger(size) || size <= 0) throw new Error('Context size must be a positive integer.');
  if (!model.trim()) throw new Error('Model identifier cannot be empty.');
  const caps = capabilities ? capabilities.split(',').map(value => value.trim()).filter(Boolean) : [];
  if (caps.some(cap => !['image_in', 'video_in', 'thinking'].includes(cap))) throw new Error('Capabilities must be a comma-separated list of image_in, video_in, or thinking.');
  await runtime.harness.setConfig({ models: { [alias]: { provider, model, maxContextSize: size, capabilities: caps } } });
  process.stdout.write(`Model ${alias} saved.\n`);
}

export async function setDefaultModel(runtime: Runtime, alias: string, secondary = false): Promise<void> {
  validName(alias);
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
