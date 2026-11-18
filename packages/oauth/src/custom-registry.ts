import {
  KIMI_CODE_PLATFORM_ID,
  KIMI_CODE_PROVIDER_NAME,
  type ManagedKimiConfigShape,
  type ManagedKimiModelAlias,
} from './managed-kimi-code';
import { CUSTOM_REGISTRY_MODEL_FIELDS, mergeRefreshedModelAlias } from './model-alias-merge';
import { OPEN_PLATFORMS } from './open-platform';
import { nonEmptyString } from './provider-credential';
import { isRecord } from './utils';

export type { ManagedKimiConfigShape };

/**
 * Identifies where a custom-registry-managed provider came from. The same
 * URL may produce multiple providers (one per top-level entry in the api.json
 * document). Refresh treats the URL as the stable registry identity and may try
 * more than one API key when existing provider records drift during key
 * rotation.
 */
export interface CustomRegistrySource {
  readonly kind: 'apiJson';
  readonly url: string;
  readonly apiKeyEnv?: string;
  /** Compatibility for catalog credentials already written by older releases. */
  readonly apiKey?: string;
}

/**
 * Parses the `source` blob parked on a provider record back into a
 * {@link CustomRegistrySource}, returning undefined when the record did not
 * come from a custom registry (manual providers, managed login, other kinds).
 * Used both to rediscover refresh candidates and to check whether a provider
 * is owned by a given registry URL.
 */
function customRegistrySourceUrl(provider: unknown): string | undefined {
  if (!isRecord(provider)) return undefined;
  const candidate = provider['source'];
  if (!isRecord(candidate) || candidate['kind'] !== 'apiJson') return undefined;
  const url = candidate['url'];
  return typeof url === 'string' && url.length > 0 ? url : undefined;
}

export function readCustomRegistrySource(provider: unknown): CustomRegistrySource | undefined {
  if (!isRecord(provider)) return undefined;
  const candidate = provider['source'];
  const url = customRegistrySourceUrl(provider);
  if (url === undefined || !isRecord(candidate)) return undefined;
  const apiKeyEnv = nonEmptyString(candidate['apiKeyEnv']);
  const apiKey = apiKeyEnv === undefined ? nonEmptyString(candidate['apiKey']) : undefined;
  return { kind: 'apiJson', url, apiKeyEnv, apiKey };
}

export function isRegistryCredential(provider: unknown, apiKey: string, apiKeyEnv?: string): boolean {
  const source = readCustomRegistrySource(provider);
  if (source === undefined) return false;
  const legacyKey = isRecord(provider) && isRecord(provider['source'])
    ? nonEmptyString(provider['source']['apiKey'])
    : undefined;
  return apiKey === legacyKey || (source.apiKeyEnv !== undefined
    && (apiKeyEnv === source.apiKeyEnv || apiKey === nonEmptyString(process.env[source.apiKeyEnv])));
}

export interface FetchCustomRegistryOptions {
  readonly signal?: AbortSignal;
  readonly fetchImpl?: typeof fetch;
  readonly userAgent?: string;
  readonly apiKey?: string;
}

const REGISTRY_TIMEOUT_MS = 30_000;
const REGISTRY_BODY_LIMIT = 8 * 1024 * 1024;

/**
 * The kosong `ProviderConfig` union (`packages/kosong/src/providers/index.ts`)
 * mirrors these literal values. `kimi` is included because the api.json schema
 * permits it even though kokub itself only emits the other three.
 */
export type CustomRegistryProviderType =
  | 'anthropic'
  | 'openai'
  | 'openai_responses'
  | 'kimi';

export interface CustomRegistryModelUpgrade {
  readonly model: string;
  readonly migration_markdown: string;
  readonly retirement_at?: string | null;
}

export interface CustomRegistryModelEntry {
  readonly id: string;
  readonly name?: string;
  readonly limit?: { context?: number; output?: number };
  readonly tool_call?: boolean;
  readonly reasoning?: boolean;
  readonly modalities?: {
    input?: readonly string[];
    output?: readonly string[];
  };
  readonly support_efforts?: readonly string[];
  readonly default_effort?: string;
  readonly upgrade?: CustomRegistryModelUpgrade;
}

export interface CustomRegistryProviderEntry {
  readonly id: string;
  readonly name: string;
  readonly api: string;
  readonly type: CustomRegistryProviderType;
  readonly env?: readonly string[];
  readonly models: Record<string, CustomRegistryModelEntry>;
}

/**
 * Tuned slightly below typical real values so the local compactor kicks in
 * before the upstream rejects with a context-overflow 4xx. Users can override
 * by editing `~/.kimi-code/config.toml`.
 */
export const CUSTOM_REGISTRY_DEFAULT_MAX_CONTEXT = 131072;
export const CUSTOM_REGISTRY_DEFAULT_CAPABILITIES = ['tool_use'] as const;

const ALLOWED_PROVIDER_TYPES: ReadonlySet<CustomRegistryProviderType> = new Set([
  'anthropic',
  'openai',
  'openai_responses',
  'kimi',
]);

const RESERVED_PROVIDER_IDS: ReadonlySet<string> = new Set([
  KIMI_CODE_PLATFORM_ID,
  KIMI_CODE_PROVIDER_NAME,
  ...OPEN_PLATFORMS.map((platform) => platform.id),
]);

/**
 * Provider ids a custom registry may never claim: the managed Kimi Code slots
 * and the first-party open platforms. Shared by the import-time guard and the
 * refresh orchestrator so the rejection rule — and its message — lives in one
 * place.
 */
export function isReservedProviderId(providerId: string): boolean {
  return RESERVED_PROVIDER_IDS.has(providerId) || providerId.startsWith('__') || providerId in Object.prototype;
}

export function reservedProviderIdMessage(providerId: string): string {
  return `Custom registry provider id "${providerId}" is reserved by Kimi Code.`;
}

export function oauthManagedProviderMessage(providerId: string): string {
  return `Custom registry provider "${providerId}" is managed by OAuth; log out before importing it.`;
}

function assertCustomRegistryProviderId(providerId: string): void {
  if (isReservedProviderId(providerId)) {
    throw new Error(reservedProviderIdMessage(providerId));
  }
}

function normalizedEndpoint(value: unknown): string | undefined {
  const text = nonEmptyString(value);
  if (text === undefined) return undefined;
  try {
    const url = new URL(text);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash) return undefined;
    url.pathname = url.pathname.replace(/\/+$/, '');
    return url.href;
  } catch {
    return undefined;
  }
}

export class CustomRegistryApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'CustomRegistryApiError';
    this.status = status;
  }
}

function isAllowedProviderType(value: unknown): value is CustomRegistryProviderType {
  return typeof value === 'string' && ALLOWED_PROVIDER_TYPES.has(value as CustomRegistryProviderType);
}

function toStringArrayOrUndefined(value: unknown): readonly string[] | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value)) return undefined;
  const out: string[] = [];
  for (const item of value) {
    if (typeof item !== 'string') return undefined;
    out.push(item);
  }
  return out;
}

const RFC3339_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;

function isValidRetirementAt(value: unknown): value is string | null | undefined {
  return (
    value === undefined ||
    value === null ||
    (typeof value === 'string' && RFC3339_PATTERN.test(value) && Number.isFinite(Date.parse(value)))
  );
}

function toModelEntry(value: unknown): CustomRegistryModelEntry | undefined {
  if (!isRecord(value)) return undefined;
  const id = value['id'];
  if (typeof id !== 'string' || id.length === 0) return undefined;

  const entry: {
    id: string;
    name?: string;
    limit?: { context?: number; output?: number };
    tool_call?: boolean;
    reasoning?: boolean;
    modalities?: { input?: readonly string[]; output?: readonly string[] };
    support_efforts?: readonly string[];
    default_effort?: string;
    upgrade?: CustomRegistryModelUpgrade;
  } = { id };

  const name = value['name'];
  if (typeof name === 'string' && name.length > 0) entry.name = name;

  const limit = value['limit'];
  if (isRecord(limit)) {
    const context = limit['context'];
    const output = limit['output'];
    const parsedLimit: { context?: number; output?: number } = {};
    if (typeof context === 'number' && Number.isFinite(context) && context > 0) {
      parsedLimit.context = Math.floor(context);
    }
    if (typeof output === 'number' && Number.isFinite(output) && output > 0) {
      parsedLimit.output = Math.floor(output);
    }
    if (parsedLimit.context !== undefined || parsedLimit.output !== undefined) {
      entry.limit = parsedLimit;
    }
  }

  if (typeof value['tool_call'] === 'boolean') entry.tool_call = value['tool_call'];
  if (typeof value['reasoning'] === 'boolean') entry.reasoning = value['reasoning'];

  const supportEfforts = toStringArrayOrUndefined(value['support_efforts']);
  if (supportEfforts !== undefined) entry.support_efforts = supportEfforts;
  const defaultEffort = value['default_effort'];
  if (typeof defaultEffort === 'string' && defaultEffort.length > 0) {
    entry.default_effort = defaultEffort;
  }

  const modalities = value['modalities'];
  if (isRecord(modalities)) {
    const input = toStringArrayOrUndefined(modalities['input']);
    const output = toStringArrayOrUndefined(modalities['output']);
    if (input !== undefined || output !== undefined) {
      entry.modalities = {
        ...(input !== undefined ? { input } : {}),
        ...(output !== undefined ? { output } : {}),
      };
    }
  }

  const rawUpgrade = value['upgrade'];
  if (rawUpgrade !== undefined && rawUpgrade !== null) {
    if (!isRecord(rawUpgrade)) {
      throw new Error(`Invalid upgrade for model "${id}": expected an object.`);
    }
    const targetModel = nonEmptyString(rawUpgrade['model']);
    if (targetModel === undefined) {
      throw new Error(`Invalid upgrade for model "${id}": expected a non-empty target model.`);
    }
    const migrationMarkdown = rawUpgrade['migration_markdown'];
    if (typeof migrationMarkdown !== 'string') {
      throw new TypeError(`Invalid upgrade for model "${id}": migration_markdown must be a string.`);
    }
    const retirementAt = rawUpgrade['retirement_at'];
    if (!isValidRetirementAt(retirementAt)) {
      throw new Error(`Invalid upgrade for model "${id}": retirement_at must be an RFC3339 timestamp or null.`);
    }
    entry.upgrade = {
      model: targetModel,
      migration_markdown: migrationMarkdown,
      retirement_at: retirementAt,
    };
  }

  return entry;
}

function toProviderEntry(value: unknown): CustomRegistryProviderEntry | undefined {
  if (!isRecord(value)) return undefined;
  const id = value['id'];
  const name = value['name'];
  const api = value['api'];
  const type = value['type'];
  const models = value['models'];

  if (typeof id !== 'string' || id.length === 0) return undefined;
  if (typeof name !== 'string' || name.length === 0) return undefined;
  if (typeof api !== 'string' || api.length === 0) return undefined;
  if (!isAllowedProviderType(type)) return undefined;
  if (!isRecord(models)) return undefined;

  const parsedModels: Record<string, CustomRegistryModelEntry> = {};
  for (const [key, raw] of Object.entries(models)) {
    const modelEntry = toModelEntry(raw);
    if (modelEntry === undefined) continue;
    parsedModels[key] = modelEntry;
  }

  const env = toStringArrayOrUndefined(value['env']);

  return {
    id,
    name,
    api,
    type,
    ...(env !== undefined ? { env } : {}),
    models: parsedModels,
  };
}

/**
 * Fetches and validates an api.json document. The returned record is keyed by
 * the top-level provider key in the document (which may differ from
 * `entry.id`); callers should iterate `Object.values` to apply each entry.
 *
 * `userAgent` identifies the host product (e.g. `kimi-code-cli/1.2.3`); when
 * omitted the request falls back to the runtime default (`User-Agent: node`).
 */
export async function fetchCustomRegistry(
  source: CustomRegistrySource,
  options: FetchCustomRegistryOptions = {},
): Promise<Record<string, CustomRegistryProviderEntry>> {
  const { signal, fetchImpl = fetch, userAgent } = options;
  const deadline = AbortSignal.timeout(REGISTRY_TIMEOUT_MS);
  const requestSignal = signal === undefined ? deadline : AbortSignal.any([signal, deadline]);
  if (normalizedEndpoint(source.url) === undefined) throw new Error('Registry URL must be HTTP(S) without embedded credentials or a fragment.');
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };
  if (userAgent !== undefined) {
    headers['User-Agent'] = userAgent;
  }
  const apiKey = options.apiKey ?? (source.apiKeyEnv === undefined
    ? source.apiKey
    : nonEmptyString(process.env[source.apiKeyEnv]));
  if (source.apiKeyEnv !== undefined && apiKey === undefined) {
    throw new Error(`Set ${source.apiKeyEnv} before fetching this registry.`);
  }
  if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`;

  let payload: unknown;
  try {
    const response = await fetchImpl(source.url, { headers, signal: requestSignal, redirect: 'error' });
    if (!response.ok) {
      void response.body?.cancel().catch(() => {});
      throw new CustomRegistryApiError(`Registry returned HTTP ${response.status}.`, response.status);
    }
    payload = await readRegistryBody(response, requestSignal);
  } catch (error) {
    signal?.throwIfAborted();
    if (deadline.aborted) throw new Error('Registry request timed out.');
    if (error instanceof CustomRegistryApiError) throw error;
    throw new Error('Could not load the registry. Check the URL, credentials, and response size (limit 8 MiB).');
  }

  if (!isRecord(payload)) {
    throw new Error('Unexpected registry response. Expected a JSON object keyed by provider ID.');
  }

  const out: Record<string, CustomRegistryProviderEntry> = {};
  for (const [key, raw] of Object.entries(payload)) {
    const entry = toProviderEntry(raw);
    if (entry === undefined) {
      // Skip invalid/unknown provider entries instead of aborting the whole
      // fetch, mirroring `toModelEntry`'s skip-on-invalid behavior. This keeps
      // existing providers working when kokub adds a new provider type that
      // this client doesn't yet recognize.
      continue;
    }
    out[key] = entry;
  }

  return out;
}

async function readRegistryBody(response: Response, signal: AbortSignal): Promise<unknown> {
  if (response.body === null) throw new Error('Empty registry response.');
  if (Number(response.headers.get('content-length')) > REGISTRY_BODY_LIMIT) {
    void response.body.cancel().catch(() => {});
    throw new Error('Registry response exceeded the size limit.');
  }
  const reader = response.body.getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  const decoder = new TextDecoder();
  let total = 0;
  let json = '';
  try {
    signal.throwIfAborted();
    for (;;) {
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) break;
      total += value.byteLength;
      if (total > REGISTRY_BODY_LIMIT) throw new Error('Registry response exceeded the size limit.');
      json += decoder.decode(value, { stream: true });
    }
    return JSON.parse(json + decoder.decode());
  } finally {
    cancel();
    signal.removeEventListener('abort', cancel);
    reader.releaseLock();
  }
}

/**
 * Derives kosong capability strings from the rich (optional) fields on a
 * custom-registry model entry. Returns an empty array when none of the rich
 * fields are present; callers are responsible for substituting the default
 * (`CUSTOM_REGISTRY_DEFAULT_CAPABILITIES`) when this returns `[]`.
 */
export function capabilitiesFromCustomEntry(model: CustomRegistryModelEntry): string[] {
  const caps = new Set<string>();
  if (model.tool_call === true) caps.add('tool_use');
  // Declaring concrete effort levels implies thinking support even when the
  // legacy `reasoning` boolean is absent.
  if (model.reasoning === true || (model.support_efforts?.length ?? 0) > 0) {
    caps.add('thinking');
  }
  if (model.modalities?.input?.includes('image') === true) caps.add('image_in');
  if (model.modalities?.input?.includes('video') === true) caps.add('video_in');
  if (model.modalities?.output?.includes('image') === true) caps.add('image_out');
  if (model.modalities?.output?.includes('audio') === true) caps.add('audio_out');
  return [...caps];
}

function hasRichCapabilityHints(model: CustomRegistryModelEntry): boolean {
  return (
    typeof model.tool_call === 'boolean' ||
    typeof model.reasoning === 'boolean' ||
    model.modalities !== undefined ||
    model.support_efforts !== undefined
  );
}

function resolveMaxContextSize(model: CustomRegistryModelEntry): number {
  const context = model.limit?.context;
  const output = model.limit?.output;
  if (typeof context === 'number' && Number.isInteger(context) && context > 0) {
    return context;
  }
  if (typeof output === 'number' && Number.isInteger(output) && output > 0) {
    return output;
  }
  return CUSTOM_REGISTRY_DEFAULT_MAX_CONTEXT;
}

function resolveCapabilities(model: CustomRegistryModelEntry): string[] {
  if (hasRichCapabilityHints(model)) {
    return capabilitiesFromCustomEntry(model);
  }
  return [...CUSTOM_REGISTRY_DEFAULT_CAPABILITIES];
}

function checkRegistryProvider(
  config: ManagedKimiConfigShape,
  entry: CustomRegistryProviderEntry,
  source: CustomRegistrySource,
  existing: unknown = config.providers[entry.id],
): void {
  assertCustomRegistryProviderId(entry.id);
  const endpoint = normalizedEndpoint(entry.api);
  if (endpoint === undefined) throw new Error(`Registry provider "${entry.id}" has an invalid inference endpoint.`);
  if (isRecord(existing) && existing['oauth'] !== undefined) {
    throw new Error(oauthManagedProviderMessage(entry.id));
  }
  if (existing !== undefined && readCustomRegistrySource(existing)?.url !== source.url) {
    throw new Error(`Registry provider "${entry.id}" conflicts with a provider owned by a different source.`);
  }
  if (
    isRecord(existing) &&
    (normalizedEndpoint(existing['baseUrl']) !== endpoint || existing['type'] !== entry.type)
  ) {
    throw new Error(`Registry provider "${entry.id}" changed its endpoint or protocol. Verify and update the provider configuration before retrying.`);
  }
  for (const modelKey of Object.keys(entry.models)) {
    const alias = `${entry.id}/${modelKey}`;
    const previous = config.models?.[alias];
    if (previous !== undefined && (existing === undefined || !isRecord(previous) || (previous['providerId'] ?? previous['provider']) !== entry.id)) {
      throw new Error(`Registry model "${alias}" conflicts with a locally configured alias.`);
    }
  }
}

export function applyCustomRegistryProvider(
  config: ManagedKimiConfigShape,
  entry: CustomRegistryProviderEntry,
  source: CustomRegistrySource,
  priorProviders?: Readonly<Record<string, unknown>>,
): void {
  const providerKey = entry.id;
  const existing = priorProviders?.[providerKey] ?? config.providers[providerKey];
  checkRegistryProvider(config, entry, source, existing);
  const priorSource = readCustomRegistrySource(existing);
  const next: Record<string, unknown> = {
    ...(isRecord(existing) ? existing : {}),
    type: entry.type,
    baseUrl: entry.api,
    source: source.apiKeyEnv !== undefined
      ? { kind: source.kind, url: source.url, apiKeyEnv: source.apiKeyEnv }
      : { kind: source.kind, url: source.url, ...(priorSource?.apiKey ? { apiKey: priorSource.apiKey } : {}) },
  };
  const previousKey = nonEmptyString(next['apiKey']);
  if (previousKey !== undefined && isRegistryCredential(existing, previousKey)) delete next['apiKey'];
  config.providers[providerKey] = next;

  const existingModels = config.models ?? {};
  // Selectively merge upstream models into the existing config so any fields
  // the user added by hand (or that upstream does not declare) survive a
  // refresh. Models that upstream no longer lists are removed; the rest are
  // merged field-by-field.
  const upstreamKeys = new Set(
    Object.keys(entry.models).map((modelKey) => `${providerKey}/${modelKey}`),
  );
  for (const [key, alias] of Object.entries(existingModels)) {
    if (isRegistryAlias(key, alias, providerKey) && !upstreamKeys.has(key)) {
      delete existingModels[key];
    }
  }

  const modelEntries = Object.entries(entry.models);
  for (const [modelKey, model] of modelEntries) {
    const aliasKey = `${providerKey}/${modelKey}`;
    const maxContextSize = resolveMaxContextSize(model);
    const capabilities = resolveCapabilities(model);
    const displayName =
      typeof model.name === 'string' && model.name.length > 0 ? model.name : model.id;
    const existing = isRecord(existingModels[aliasKey]) ? existingModels[aliasKey] : {};
    const upgrade = model.upgrade;
    const target =
      upgrade === undefined
        ? undefined
        : modelEntries.find(([, candidate]) => candidate.id === upgrade.model);
    const targetAlias =
      target === undefined || target[1].id === model.id ? undefined : `${providerKey}/${target[0]}`;

    const remoteAlias: ManagedKimiModelAlias = {
      provider: providerKey,
      model: model.id,
      maxContextSize,
      capabilities,
      displayName,
      supportEfforts: model.support_efforts,
      defaultEffort: model.default_effort,
      // `{model_from}` / `{model_to}` belong to the registry format, so store
      // the final text: every client then renders the same explanation.
      upgrade:
        targetAlias === undefined || upgrade === undefined
          ? undefined
          : {
              model: targetAlias,
              migrationMarkdown: upgrade.migration_markdown
                .replaceAll('{model_from}', model.id)
                .replaceAll('{model_to}', upgrade.model),
              retirementAt: upgrade.retirement_at ?? undefined,
            },
    };
    existingModels[aliasKey] = mergeRefreshedModelAlias(
      existing,
      remoteAlias,
      CUSTOM_REGISTRY_MODEL_FIELDS,
    );
  }

  config.models = existingModels;
}

function isRegistryAlias(key: string, alias: unknown, providerId: string): boolean {
  return key.startsWith(`${providerId}/`) && isRecord(alias) && (alias['providerId'] ?? alias['provider']) === providerId;
}

/**
 * Removes a custom-registry provider and every model alias that referenced it.
 * Clears `defaultModel` if it pointed at a removed alias. Mirrors
 * `removeOpenPlatformConfig`.
 */
export function removeCustomRegistryProvider(
  config: ManagedKimiConfigShape,
  providerId: string,
): void {
  delete config.providers[providerId];

  let removedDefault = false;
  const existingModels = config.models ?? {};
  for (const [key, alias] of Object.entries(existingModels)) {
    if (!isRecord(alias) || (alias['providerId'] ?? alias['provider']) !== providerId) continue;
    delete existingModels[key];
    if (config.defaultModel === key) removedDefault = true;
  }
  config.models = existingModels;

  if (removedDefault) {
    config.defaultModel = undefined;
  }

  if (config['defaultProvider'] === providerId) {
    config['defaultProvider'] = undefined;
  }
}

/**
 * The replacement alias that a stored model alias declared through registry
 * `upgrade` metadata. Registries retire a model by dropping it after declaring
 * this, so callers follow it when the alias it belongs to disappears.
 */
export function declaredUpgradeTarget(
  config: ManagedKimiConfigShape,
  alias: string | undefined,
): string | undefined {
  const model = alias === undefined ? undefined : config.models?.[alias];
  return isRecord(model) && isRecord(model['upgrade'])
    ? nonEmptyString(model['upgrade']['model'])
    : undefined;
}

/**
 * Surfaces the credential env var each entry declares via its `env` field, as
 * `{ [entry.id]: varName }` — a hint only. The binding is never applied
 * automatically: the registry controls both the variable name and the endpoint
 * the credential is sent to, so the user must opt in by setting `api_key_env`
 * in config.toml.
 */
export function credentialEnvHints(
  entries: readonly CustomRegistryProviderEntry[],
): Record<string, string> {
  const hints: Record<string, string> = {};
  for (const entry of entries) {
    for (const value of entry.env ?? []) {
      const envName = nonEmptyString(value);
      if (envName !== undefined) {
        hints[entry.id] = envName;
        break;
      }
    }
  }
  return hints;
}

export interface CustomRegistryRemoval {
  readonly prior: Readonly<Record<string, unknown>>;
  readonly previousDefault: string | undefined;
  readonly previousDefaultUpgrade: string | undefined;
  readonly previousDefaultProvider: string | undefined;
}

/**
 * Remove providers that disappeared from their owning registry. Capture the
 * prior selection so a declared upgrade can replace a retired default.
 */
export function removeCustomRegistryEntries(
  config: ManagedKimiConfigShape,
  entries: Record<string, CustomRegistryProviderEntry>,
  source: CustomRegistrySource,
): CustomRegistryRemoval {
  const surviving = new Set(Object.values(entries).map((entry) => entry.id));
  const removal: CustomRegistryRemoval = {
    prior: { ...config.providers },
    previousDefault: config.defaultModel,
    previousDefaultUpgrade: declaredUpgradeTarget(config, config.defaultModel),
    previousDefaultProvider: config['defaultProvider'] as string | undefined,
  };
  for (const [providerId, provider] of Object.entries(config.providers)) {
    if (!isRecord(provider)) continue;
    if (provider['oauth'] !== undefined || isReservedProviderId(providerId)) continue;
    if (!surviving.has(providerId) && customRegistrySourceUrl(provider) === source.url) {
      removeCustomRegistryProvider(config, providerId);
    }
  }
  return removal;
}

export type CustomRegistryReplacementKeys = Readonly<Record<string, readonly string[]>>;

export function customRegistryReplacementKeys(
  config: ManagedKimiConfigShape,
  entries: Record<string, CustomRegistryProviderEntry>,
  source: CustomRegistrySource,
): CustomRegistryReplacementKeys {
  const providerIds = new Set(Object.values(entries).map((entry) => entry.id));
  const retiredIds = new Set<string>();
  for (const [providerId, provider] of Object.entries(config.providers)) {
    if (!isRecord(provider)) continue;
    if (provider['oauth'] !== undefined || isReservedProviderId(providerId)) continue;
    if (customRegistrySourceUrl(provider) === source.url && !providerIds.has(providerId)) retiredIds.add(providerId);
  }
  const modelIds = new Set<string>();
  for (const [modelId, model] of Object.entries(config.models ?? {})) {
    if (!isRecord(model)) continue;
    const owner = model['providerId'] ?? model['provider'];
    if (typeof owner === 'string' && (retiredIds.has(owner) || (providerIds.has(owner) && isRegistryAlias(modelId, model, owner)))) {
      modelIds.add(modelId);
    }
  }
  for (const entry of Object.values(entries)) {
    for (const modelId of Object.keys(entry.models)) {
      modelIds.add(`${entry.id}/${modelId}`);
    }
  }
  return {
    providers: [...providerIds, ...retiredIds],
    models: [...modelIds],
    thinking: ['enabled', 'effort', 'keep'],
  };
}

/**
 * Applies every entry from a single api.json import in memory. Pass the
 * snapshot returned by {@link removeCustomRegistryEntries} when a caller has
 * already removed the records in memory; without it, the removals are
 * performed here first.
 *
 * Re-import semantics: providers previously imported from the same source URL
 * but no longer present in `entries` are removed (along with their aliases and
 * any `defaultModel` pointing at them). Without this, deleting a provider
 * upstream and re-importing the registry leaves orphaned provider records and
 * model aliases behind. Matching is by `source.url` only — the apiKey commonly
 * rotates between imports, but the URL is the stable identity of "the same
 * registry".
 *
 * Surviving entries keep local settings and manually created aliases. Defaults
 * that still resolve are restored from the snapshot; a `defaultModel` left
 * dangling moves to the replacement it declared through `upgrade` when that
 * still resolves, keeping `thinking`, and is cleared together with `thinking`
 * otherwise (mirroring the refresh path).
 */
export function applyCustomRegistryEntries(
  config: ManagedKimiConfigShape,
  entries: Record<string, CustomRegistryProviderEntry>,
  source: CustomRegistrySource,
  removal?: CustomRegistryRemoval,
): void {
  const entryList = Object.values(entries);
  const priorProviders = removal?.prior ?? config.providers;
  const seen = new Set<string>();
  for (const entry of entryList) {
    if (seen.has(entry.id)) throw new Error(`Registry contains duplicate provider ID "${entry.id}".`);
    seen.add(entry.id);
    checkRegistryProvider(config, entry, source, priorProviders[entry.id]);
  }
  const r = removal ?? removeCustomRegistryEntries(config, entries, source);
  for (const entry of entryList) {
    applyCustomRegistryProvider(config, entry, source, r.prior);
  }

  config.defaultModel = [r.previousDefault, r.previousDefaultUpgrade].find(
    (alias) => alias !== undefined && config.models?.[alias] !== undefined,
  );
  config['defaultProvider'] =
    r.previousDefaultProvider !== undefined &&
    config.providers[r.previousDefaultProvider] !== undefined
      ? r.previousDefaultProvider
      : undefined;
  if (r.previousDefault !== undefined && config.defaultModel === undefined) {
    config.thinking = undefined;
  }
}
