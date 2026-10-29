import type { ProviderConfig } from '@coderai/sdk';

export type DiscoveredModel = { id: string; name?: string; context?: number; capabilities: string[] };
type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const positive = (value: unknown): number | undefined => typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : undefined;

/** Fetch metadata only. Never generate a completion or include a credential in diagnostics. */
export async function discoverProviderModels(provider: ProviderConfig, signal?: AbortSignal): Promise<DiscoveredModel[]> {
  if (provider.type === 'vertexai') throw new Error('Vertex AI model discovery is unavailable. Enter a model ID manually.');
  if (!provider.baseUrl) throw new Error('This provider has no base URL. Enter a model ID manually.');
  const standard: Record<string, string> = { openai: 'OPENAI_API_KEY', openai_responses: 'OPENAI_API_KEY', kimi: 'KIMI_API_KEY', anthropic: 'ANTHROPIC_API_KEY', 'google-genai': 'GOOGLE_API_KEY' };
  const envName = provider.apiKeyEnv || standard[provider.type] || '';
  const key = provider.apiKey?.trim() || (provider.apiKeyEnv ? process.env[envName]?.trim() : provider.env?.[envName]?.trim() || process.env[envName]?.trim());
  if (!key) throw new Error(`Set ${envName || 'the provider API key'} before discovering models, or enter a model ID manually.`);
  const google = provider.type === 'google-genai';
  const anthropic = provider.type === 'anthropic';
  const base = new URL(provider.baseUrl);
  base.pathname = base.pathname.replace(/\/$/, '') + (google && !/\/v1(?:beta|alpha)?$/.test(base.pathname.replace(/\/$/, '')) ? '/v1beta' : anthropic && !/\/v1$/.test(base.pathname.replace(/\/$/, '')) ? '/v1' : '') + '/models';
  const headers: Record<string, string> = { ...provider.customHeaders };
  if (google) headers['x-goog-api-key'] = key;
  else if (anthropic) { headers['x-api-key'] = key; headers['anthropic-version'] = '2023-06-01'; }
  else headers['Authorization'] = `Bearer ${key}`;
  const models = new Map<string, DiscoveredModel>();
  const seen = new Set<string>();
  const timeout = AbortSignal.timeout(10000);
  const requestSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let cursor = '';
  for (let page = 0; page < 20; page++) {
    const url = new URL(base);
    if (google) { url.searchParams.set('pageSize', '1000'); if (cursor) url.searchParams.set('pageToken', cursor); }
    if (anthropic) { url.searchParams.set('limit', '1000'); if (cursor) url.searchParams.set('after_id', cursor); }
    let body: RecordValue;
    try {
      const response = await fetch(url, { headers, signal: requestSignal, redirect: 'error' });
      if (!response.ok) {
        await response.body?.cancel();
        throw new Error(`Model discovery returned HTTP ${response.status}. ${response.status === 401 || response.status === 403 ? 'Check the API key and provider access.' : 'Try again or enter a model ID manually.'}`);
      }
      body = record(await response.json());
    } catch (error) {
      if (signal?.aborted) throw Object.assign(new Error('Setup cancelled.'), { name: 'AbortError' });
      if (timeout.aborted) throw new Error('Model discovery timed out. Try again or enter a model ID manually.');
      // Do not expose server responses, URLs, or fetch errors that may contain secrets.
      if (error instanceof Error && error.message.startsWith('Model discovery returned HTTP ')) throw error;
      throw new Error('Could not load models from this provider. Check the endpoint or enter a model ID manually.');
    }
    const entries = google ? body['models'] : body['data'];
    if (!Array.isArray(entries)) throw new Error('The provider returned an unsupported model list. Enter a model ID manually.');
    for (const value of entries) {
      const item = record(value);
      if (google && !(Array.isArray(item['supportedGenerationMethods']) && item['supportedGenerationMethods'].includes('generateContent'))) continue;
      const rawID = google ? item['name'] : item['id'];
      if (typeof rawID !== 'string' || !rawID.trim() || /[\x00-\x1f\x7f-\x9f]/.test(rawID)) continue;
      if (item['lifecycle'] === 'retired') continue;
      const id = google ? rawID.replace(/^models\//, '') : rawID;
      const capabilities: string[] = [];
      const modalities = item['input_modalities'];
      const caps = record(item['capabilities']);
      if ((Array.isArray(modalities) && modalities.includes('image')) || record(caps['image_input'])['supported'] === true) capabilities.push('image_in');
      if (Array.isArray(modalities) && modalities.includes('video')) capabilities.push('video_in');
      if (record(caps['thinking'])['supported'] === true || (Array.isArray(record(item['effort'])['supported_levels']) && (record(item['effort'])['supported_levels'] as unknown[]).length > 0)) capabilities.push('thinking');
      const name = item['displayName'] ?? item['display_name'] ?? item['name'];
      models.set(id, { id, ...(typeof name === 'string' ? { name } : {}), context: positive(item['inputTokenLimit'] ?? item['max_input_tokens'] ?? item['context_window']), capabilities });
    }
    cursor = google && typeof body['nextPageToken'] === 'string' ? body['nextPageToken'] : anthropic && body['has_more'] === true && typeof body['last_id'] === 'string' ? body['last_id'] : '';
    if (!cursor) break;
    if (seen.has(cursor) || page === 19) throw new Error('The provider returned too many model pages. Enter a model ID manually.');
    seen.add(cursor);
  }
  return [...models.values()].sort((a, b) => a.id.localeCompare(b.id));
}
