import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { register } from 'tsx/esm/api';
register();
const { discoverProviderModels } = await import('../apps/coderai/src/setup-models.ts');

async function serverFixture(t, handler) {
  const requests = [];
  const server = createServer((req, res) => {
    requests.push({ url: req.url, headers: req.headers });
    res.setHeader('Content-Type', 'application/json');
    handler(req, res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  return { baseUrl: `http://127.0.0.1:${server.address().port}`, requests };
}

test('OpenAI-compatible discovery uses the exact configured prefix and reads DeepSeek metadata', async t => {
  const f = await serverFixture(t, (_req, res) => res.end(JSON.stringify({ data: [
    { id: 'zeta' },
    { id: 'alpha', name: 'Alpha model', context_window: 128000, input_modalities: ['text', 'image'], effort: { supported_levels: ['high'] } },
    { id: 'alpha', context_window: 256000, input_modalities: ['text', 'image'], effort: { supported_levels: ['high'] } },
    { id: 'escape\u001b[2J' }, { id: '' }, null,
  ] })));
  const models = await discoverProviderModels({ type: 'openai', baseUrl: `${f.baseUrl}/v1/`, apiKey: 'secret-value', customHeaders: { 'x-test': 'present' } });
  assert.deepEqual(models.map(model => model.id), ['alpha', 'zeta']);
  assert.equal(models[0].context, 256000);
  assert.deepEqual(models[0].capabilities, ['image_in', 'thinking']);
  assert.equal(models[1].context, undefined);
  assert.equal(f.requests[0].url, '/v1/models');
  assert.equal(f.requests[0].headers.authorization, 'Bearer secret-value');
  assert.equal(f.requests[0].headers['x-test'], 'present');
});

test('Anthropic discovery adds /v1 once, paginates, and reads token limits', async t => {
  const f = await serverFixture(t, (req, res) => res.end(JSON.stringify(req.url.includes('after_id') ? {
    data: [{ id: 'second', display_name: 'Second', max_input_tokens: 100000, capabilities: { image_input: { supported: true }, thinking: { supported: true } } }], has_more: false,
  } : { data: [{ id: 'first' }], has_more: true, last_id: 'first' })));
  for (const path of ['', '/v1/']) {
    const models = await discoverProviderModels({ type: 'anthropic', baseUrl: f.baseUrl + path, apiKey: 'test-key' });
    assert.deepEqual(models.map(model => model.id), ['first', 'second']);
    assert.equal(models[1].context, 100000);
    assert.deepEqual(models[1].capabilities, ['image_in', 'thinking']);
  }
  assert.ok(f.requests.every(req => req.url.startsWith('/v1/models?')));
  assert.equal(f.requests[0].headers['x-api-key'], 'test-key');
  assert.equal(f.requests[0].headers['anthropic-version'], '2023-06-01');
  assert.match(f.requests[1].url, /after_id=first/);
});

test('Google discovery strips models prefix, filters embedding models, and follows page tokens', async t => {
  const f = await serverFixture(t, (req, res) => res.end(JSON.stringify(req.url.includes('pageToken') ? {
    models: [{ name: 'models/second', supportedGenerationMethods: ['generateContent'], inputTokenLimit: 200000 }],
  } : { models: [
    { name: 'models/first', displayName: 'First', supportedGenerationMethods: ['generateContent'], inputTokenLimit: 100000 },
    { name: 'models/embedding', supportedGenerationMethods: ['embedContent'] },
  ], nextPageToken: 'next-page' })));
  for (const path of ['', '/v1beta/']) {
    const models = await discoverProviderModels({ type: 'google-genai', baseUrl: f.baseUrl + path, apiKey: 'google-secret' });
    assert.deepEqual(models.map(model => model.id), ['first', 'second']);
    assert.equal(models[0].context, 100000);
  }
  assert.ok(f.requests.every(req => req.url.startsWith('/v1beta/models?') && !req.url.includes('google-secret')));
  assert.equal(f.requests[0].headers['x-goog-api-key'], 'google-secret');
});

test('HTTP errors, malformed responses and redirects expose no credential or response text', async t => {
  for (const status of [401, 403, 404, 500, 302, 200]) {
    const f = await serverFixture(t, (_req, res) => {
      res.statusCode = status;
      if (status === 302) res.setHeader('Location', 'https://example.com?key=LEAKED_SECRET');
      res.end('LEAKED_SECRET');
    });
    await assert.rejects(discoverProviderModels({ type: 'openai', baseUrl: f.baseUrl, apiKey: 'LEAKED_SECRET' }), error => {
      assert.doesNotMatch(error.message, /LEAKED_SECRET|127\.0\.0\.1|example\.com/);
      return true;
    });
  }
});

test('discovery rejects malformed and repeating pages and honors cancellation', async t => {
  const malformed = await serverFixture(t, (_req, res) => res.end(JSON.stringify({ unexpected: [] })));
  await assert.rejects(discoverProviderModels({ type: 'openai', baseUrl: malformed.baseUrl, apiKey: 'key' }), /unsupported model list/);
  const repeating = await serverFixture(t, (_req, res) => res.end(JSON.stringify({ data: [{ id: 'same' }], has_more: true, last_id: 'same' })));
  await assert.rejects(discoverProviderModels({ type: 'anthropic', baseUrl: repeating.baseUrl, apiKey: 'key' }), /too many model pages/);
  const pending = await serverFixture(t, () => {});
  const controller = new AbortController();
  const result = discoverProviderModels({ type: 'openai', baseUrl: pending.baseUrl, apiKey: 'key' }, controller.signal);
  controller.abort();
  await assert.rejects(result, { name: 'AbortError' });
});

test('missing environment credentials and Vertex AI offer manual model entry', async () => {
  await assert.rejects(discoverProviderModels({ type: 'openai', baseUrl: 'https://example.com', apiKeyEnv: 'CODERAI_TEST_UNSET_KEY_746125' }), /Set CODERAI_TEST_UNSET_KEY_746125/);
  await assert.rejects(discoverProviderModels({ type: 'vertexai', baseUrl: 'https://example.com' }), /Enter a model ID manually/);
});
