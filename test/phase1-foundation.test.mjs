import { test, after } from 'node:test';
import { stringify } from 'smol-toml';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { build } from 'esbuild';
import { fixture } from './fixture.mjs';
const { runSetup } = await import('../apps/coderai/src/setup.ts');
const { setupMenuLines } = await import('../apps/coderai/src/setup-terminal.ts');

// Use the production bundler's alias/asset rules to exercise the SDK and
// retained engine together. Only Google's transport is replaced, so its
// constructor options can be checked without cloud credentials.
const dir = await mkdtemp(resolve('node_modules/.phase1-'));
const entry = join(dir, 'foundation.mjs');
await build({
  stdin: { resolveDir: process.cwd(), contents: `
    export * from './packages/node-sdk/src/index.ts';
    export * from './packages/node-sdk/src/config/schema.ts';
    export * from './packages/node-sdk/src/config/toml.ts';
    export * from './packages/node-sdk/src/v2/config-mapper.ts';
    export * from './packages/agent-core-v2/src/llm-adapter/protocol/protocolAdapterRegistry.ts';
    export * from './packages/agent-core-v2/src/llm-adapter/model/model-auth.ts';
    export * from './packages/agent-core-v2/src/llm-adapter/model/catalog-service.ts';
    export * from './packages/agent-core-v2/src/llm-adapter/model/catalog-runtime.ts';
    export * from './packages/agent-core-v2/src/llm-adapter/provider/provider-definition.ts';
    export * from './packages/agent-core-v2/src/features/externalHooks/internal/types.ts';
    export * from './packages/agent-core-v2/src/human/llm/requester/bases/anthropic/requester.ts';
    export * from './packages/agent-core-v2/src/agent/media/mediaRef.ts';
    export * from './packages/agent-core-v2/src/agent/tools/read-media-file/readMediaFileTool.ts';
    export * from './packages/klient/src/contract/agent/schemas.ts';
    export { clients } from '@google/genai';
  ` },
  outfile: entry, bundle: true, platform: 'node', format: 'esm', target: 'node24',
  tsconfigRaw: { compilerOptions: { experimentalDecorators: true } },
  plugins: [{ name: 'engine-aliases', setup(b) {
    b.onResolve({ filter: /^#/ }, async args => {
      let root = args.resolveDir;
      while (!existsSync(join(root, 'package.json'))) root = dirname(root);
      const path = args.path.startsWith('#human/')
        ? resolve('packages/agent-core-v2/src/human', args.path.slice(7))
        : join(root, basename(root) === 'human' ? '' : 'src', args.path.slice(2));
      return b.resolve(path, { resolveDir: args.resolveDir, kind: args.kind });
    });
    b.onResolve({ filter: /\?raw$/ }, args => ({ path: resolve(args.resolveDir, args.path.slice(0, -4)), namespace: 'raw' }));
    b.onLoad({ filter: /.*/, namespace: 'raw' }, async args => ({ contents: await readFile(args.path, 'utf8'), loader: 'text' }));
    b.onResolve({ filter: /^@google\/genai$/ }, () => ({ path: 'google', namespace: 'fake' }));
    b.onLoad({ filter: /.*/, namespace: 'fake' }, () => ({ contents: `
      export class ApiError extends Error {}
      export const clients = [];
      export class GoogleGenAI {
        constructor(options) { clients.push(options); }
        models = { async generateContentStream() { return (async function* () {})(); } };
      }
    ` }));
    b.onResolve({ filter: /^[\w@][^:]*$/ }, args => {
      if (args.path.startsWith('@coderai/') || args.path.startsWith('#')) return;
      return { path: /^ajv\/dist\/(2019|2020)$/.test(args.path) ? args.path + '.js' : args.path, external: true };
    });
  } }],
});
const api = await import(entry);
api.configureTextBuildWorkerRuntime(resolve('dist/text-build-worker.mjs'));
after(() => rm(dir, { recursive: true, force: true }));

const capability = { image_in: false, video_in: false, audio_in: false, thinking: false, tool_use: true, max_context_tokens: 100000 };
const model = (protocol, options = {}) => ({ id: 'test', name: 'test-model', protocol, headers: {}, capabilities: capability, maxContextSize: 100000, ...options });

test('setup saves a custom search endpoint and keeps Finish setup visible in small terminals', async () => {
  let saved;
  const answers = ['9', 'https://search.example.test/search', 'test-search-key', '0'];
  const questions = [];
  const prompt = async (question, secret) => { questions.push({ question, secret }); return answers.shift(); };
  await runSetup({ harness: {
    configPath: '/tmp/coderai-test-config.toml',
    getConfig: async () => ({ providers: {}, models: {} }),
    setConfig: async patch => { saved = patch; },
  } }, prompt);
  assert.deepEqual(saved, { services: { moonshotSearch: { baseUrl: 'https://search.example.test/search', apiKey: 'test-search-key' } } });
  assert.ok(questions.some(item => item.question.includes('Search API key') && item.secret));
  const lines = setupMenuLines({ title: 'Setup', items: [
    ...Array.from({ length: 9 }, (_, index) => ({ label: `Option ${index + 1}`, index })), { label: 'Finish setup', index: -1 },
  ], selected: 0, query: '', shortcut: '', columns: 50, rows: 16 });
  assert.match(lines.join('\n'), /Finish setup/);
});

function catalogModel(provider, record = {}) {
  const raw = { provider: 'cloud', model: 'gemini-test', maxContextSize: 100000, ...record };
  const definition = api.toCatalogModelDefinition('test', raw, 'cloud');
  const runtime = { sync() {}, onChanged: () => ({ dispose() {} }), lookup: () => definition, providerInfo: () => provider };
  return new api.ModelCatalog(runtime, {}, {}, {}, new api.ProtocolAdapterRegistry(), {}).get('test');
}

test('catalog preserves Vertex defaults, provider overrides and environment fallback without changing Gemini routing', () => {
  const oldProject = process.env.GOOGLE_CLOUD_PROJECT;
  const oldLocation = process.env.GOOGLE_CLOUD_LOCATION;
  try {
    process.env.GOOGLE_CLOUD_PROJECT = 'environment-project';
    process.env.GOOGLE_CLOUD_LOCATION = 'global';
    assert.deepEqual(catalogModel({ type: 'vertexai' }).providerOptions, { vertexai: true, project: 'environment-project', location: 'global' });
    assert.deepEqual(catalogModel({ type: 'vertexai', project: 'explicit', location: 'us-east1' }).providerOptions, { vertexai: true, project: 'explicit', location: 'us-east1' });
    assert.deepEqual(catalogModel({ type: 'vertexai', env: { GOOGLE_CLOUD_PROJECT: 'provider-env', GOOGLE_CLOUD_LOCATION: 'us-west1' } }).providerOptions, { vertexai: true, project: 'provider-env', location: 'us-west1' });
    assert.equal(catalogModel({ type: 'google-genai', apiKey: 'test-key' }).providerOptions, undefined);
    delete process.env.GOOGLE_CLOUD_PROJECT;
    delete process.env.GOOGLE_CLOUD_LOCATION;
    assert.equal(catalogModel({ type: 'vertexai' }).providerOptions.vertexai, true);
    assert.equal(catalogModel({ type: 'google-genai', apiKey: 'test-key' }).providerOptions, undefined);
    const anthropic = catalogModel({ type: 'anthropic' }, { model: 'custom', maxOutputSize: 1234, metadata: { user_id: 'local-user' } });
    assert.equal(anthropic.providerOptions.defaultMaxTokens, 1234);
    assert.deepEqual(anthropic.providerOptions.metadata, { user_id: 'local-user' });
  } finally {
    if (oldProject === undefined) delete process.env.GOOGLE_CLOUD_PROJECT; else process.env.GOOGLE_CLOUD_PROJECT = oldProject;
    if (oldLocation === undefined) delete process.env.GOOGLE_CLOUD_LOCATION; else process.env.GOOGLE_CLOUD_LOCATION = oldLocation;
  }
});

test('Vertex provider selects Google protocol and preserves cloud options through client creation', async () => {
  assert.equal(api.resolveModelProtocol({}, { type: 'vertexai' }), 'google-genai');
  assert.deepEqual(api.getProviderDefinition('vertexai').providerOptions, { vertexai: true });
  const registry = new api.ProtocolAdapterRegistry();
  const resolved = registry.resolve(model('google-genai', {
    providerType: 'vertexai', baseUrl: 'https://vertex.example.test', headers: { 'x-test': 'value' },
    providerOptions: { vertexai: true, project: 'project-a', location: 'us-central1' },
  }));
  const events = [];
  await resolved.requester.generate({ model: resolved.model }, { messages: [] }, {
    signal: new AbortController().signal, onEvent: event => events.push(event),
  });
  assert.ok(events.some(event => event.type === 'llm.done'), JSON.stringify(events));
  assert.deepEqual(api.clients.at(-1), {
    apiKey: undefined, vertexai: true, project: 'project-a', location: 'us-central1',
    httpOptions: { baseUrl: 'https://vertex.example.test', headers: { 'x-test': 'value' } },
  });
});

test('Anthropic output budgets and metadata survive adapter resolution and request formatting', () => {
  const resolved = new api.ProtocolAdapterRegistry().resolve(model('anthropic', {
    maxOutputSize: 2345, providerOptions: { defaultMaxTokens: 2345, metadata: { user_id: 'configured-user' }, betaApi: true },
  }));
  const input = { model: resolved.model, messages: [], tools: [], cacheKey: 'cache-user' };
  const request = api.prepareAnthropicRequest(input);
  assert.equal(request.params.max_tokens, 2345);
  assert.deepEqual(request.params.metadata, { user_id: 'configured-user' });
  assert.equal(request.useBetaApi, true);
  assert.equal(api.prepareAnthropicRequest({ ...input, maxCompletionTokens: 1000 }).params.max_tokens, 1000);
  assert.equal(api.prepareAnthropicRequest({ ...input, maxContextTokens: 100000, usedContextTokens: 99900 }).params.max_tokens, 100);
});

test('all 20 engine hook events and V2 configuration domains survive SDK validation and TOML round trips', () => {
  assert.equal(api.HOOK_EVENT_TYPES.length, 20);
  const hooks = api.HOOK_EVENT_TYPES.map(event => ({ event, command: 'true' }));
  const domains = {
    experimental: { tool_select: true }, cron: { disabled: true }, tools: { disabled: ['Bash'] },
    watch: { enabled: false }, persistence: { enabled: false }, tokenCounting: { strategy: 'estimated' },
    agentIdentity: { name: 'Local' }, modelOverrides: { maxCompletionTokens: 2345 },
    extraAgentDirs: ['/tmp/agents'], identity: { name: 'CoderAI', slug: 'coderai' }, database: { search: false },
  };
  const patch = api.KimiConfigPatchSchema.parse({ ...domains, hooks });
  const projected = api.resolvedConfigToKimiConfig({ providers: {}, ...patch });
  assert.deepEqual(projected, { providers: {}, ...patch });
  const encoded = api.configToTomlData(api.KimiConfigSchema.parse(projected));
  const restored = api.parseConfigString(stringify(encoded));
  for (const [key, value] of Object.entries(domains)) assert.deepEqual(restored[key], value, key);
  assert.deepEqual(restored.hooks, hooks);
});

test('audio suffixes, daemon references and prompt validation preserve audio parts', () => {
  for (const [suffix, mime] of [['mp3', 'audio/mpeg'], ['wav', 'audio/wav'], ['ogg', 'audio/ogg'], ['m4a', 'audio/mp4'], ['flac', 'audio/flac']]) {
    assert.deepEqual(api.detectFileType(`sound.${suffix}`, Buffer.from([0, 0]), 'media'), { kind: 'audio', mimeType: mime });
  }
  const part = { type: 'audio_url', audioUrl: { url: api.buildDaemonFileUrl('audio-id') } };
  assert.deepEqual(api.daemonFileRefFromPart(part), { kind: 'audio', ref: { fileId: 'audio-id' } });
  assert.deepEqual(api.promptPartSchema.parse(part), part);
});

test('persisted cron and model overrides remain separate from temporary environment overrides', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const path = join(f.home, 'config.toml');
  await writeFile(path, '[cron]\ndisabled = false\n[model_overrides]\nmax_completion_tokens = 512\ntop_p = 0.75\n');
  const previous = { cron: process.env.KIMI_DISABLE_CRON, tokens: process.env.KIMI_MODEL_MAX_COMPLETION_TOKENS };
  let harness;
  try {
    process.env.KIMI_DISABLE_CRON = '1';
    process.env.KIMI_MODEL_MAX_COMPLETION_TOKENS = '1024';
    harness = api.createCoderaiHarness({ homeDir: f.home, configPath: path, identity: { productName: 'coderai-test', version: '0.1.0', platform: 'kimi_code_cli' } });
    const config = await harness.getConfig();
    assert.equal(config.cron.disabled, true);
    assert.equal(config.modelOverrides.maxCompletionTokens, 1024);
    assert.equal(config.modelOverrides.topP, 0.75);
    await harness.setConfig({ cron: config.cron, modelOverrides: config.modelOverrides });
    const persisted = api.parseConfigString(await readFile(path, 'utf8'));
    assert.equal(persisted.cron.disabled, false);
    assert.equal(persisted.modelOverrides.maxCompletionTokens, 512);
    assert.equal(persisted.modelOverrides.topP, 0.75);
  } finally {
    await harness?.close();
    if (previous.cron === undefined) delete process.env.KIMI_DISABLE_CRON; else process.env.KIMI_DISABLE_CRON = previous.cron;
    if (previous.tokens === undefined) delete process.env.KIMI_MODEL_MAX_COMPLETION_TOKENS; else process.env.KIMI_MODEL_MAX_COMPLETION_TOKENS = previous.tokens;
  }
});

async function turn(session, input) {
  const events = [];
  let off;
  let timer;
  const done = new Promise((resolveTurn, reject) => {
    timer = setTimeout(() => reject(new Error(`Turn timed out: ${JSON.stringify(events)}`)), 15000);
    off = session.onEvent(event => { events.push(event); if (event.type === 'turn.ended') resolveTurn(event); });
  });
  try { await session.prompt(input); await done; return events; }
  finally { clearTimeout(timer); off?.(); }
}

test('SDK custom tools execute, replace, fail, reload and unregister with session isolation', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.server.removeAllListeners('request');
  f.server.on('request', async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); f.requests.push(body);
    const call = !body.messages.some(message => message.role === 'tool');
    const delta = call
      ? { tool_calls: [{ index: 0, id: 'sdk-tool-1', type: 'function', function: { name: 'Lookup', arguments: '{"query":"hello"}' } }] }
      : { content: 'Tool complete.' };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end(`data: ${JSON.stringify({ id: 'sdk', choices: [{ index: 0, delta, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ id: 'sdk', choices: [{ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }] })}\n\ndata: [DONE]\n\n`);
  });
  const harness = api.createCoderaiHarness({ homeDir: f.home, configPath: join(f.home, 'config.toml'), identity: { productName: 'coderai-test', version: '0.1.0', platform: 'kimi_code_cli' } });
  t.after(() => harness.close());
  await harness.setConfig({
    providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } },
    models: { local: { provider: 'local', model: 'test', maxContextSize: 100000, capabilities: ['audio_in'] } }, defaultModel: 'local',
    watch: { enabled: false }, database: { search: false },
  });
  const domains = {
    experimental: { tool_select: false }, cron: { disabled: true }, tools: { disabled: ['Bash'] },
    watch: { enabled: false }, persistence: { enabled: false }, tokenCounting: { strategy: 'estimated' },
    agentIdentity: { name: 'Local' }, modelOverrides: { maxCompletionTokens: 512 },
  };
  await harness.setConfig(domains);
  const saved = await harness.getConfig({ reload: true });
  for (const [key, value] of Object.entries(domains)) {
    for (const [field, setting] of Object.entries(value)) assert.deepEqual(saved[key][field], setting, `${key}.${field}`);
  }
  await harness.trustWorkspace(f.cwd);
  const first = await harness.createSession({ workDir: f.cwd, permission: 'yolo' });
  const second = await harness.createSession({ workDir: f.cwd, permission: 'yolo' });
  await assert.rejects(first.prompt([{ type: 'audio_url', audioUrl: { url: '' } }]), /empty audio URLs/);
  const tool = { name: 'Lookup', description: 'Look up a value.', parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] } };
  let calls = 0;
  await first.registerTool(tool, (args, request) => {
    calls++; assert.equal(request.sessionId, first.id); assert.equal(request.name, 'Lookup');
    return { output: `FOUND:${args.query}` };
  });
  let events = await turn(first, 'Use Lookup');
  assert.equal(calls, 1);
  assert.ok(events.some(event => event.type === 'tool.result' && JSON.stringify(event).includes('FOUND:hello')), JSON.stringify(events));
  await first.registerTool(tool, () => { throw new Error('expected handler failure'); });
  await first.clearContext();
  events = await turn(first, 'Use Lookup again');
  assert.ok(events.some(event => event.type === 'tool.result' && JSON.stringify(event).includes('expected handler failure')), JSON.stringify(events));
  await second.registerTool(tool, () => ({ output: 'SECOND' }));
  events = await turn(second, 'Use Lookup in second session');
  assert.ok(events.some(event => event.type === 'tool.result' && JSON.stringify(event).includes('SECOND')));
  await first.registerTool(tool, () => ({ output: 'AFTER RELOAD' }));
  await first.reloadSession();
  await first.clearContext();
  events = await turn(first, 'Use Lookup after reload');
  assert.ok(events.some(event => event.type === 'tool.result' && JSON.stringify(event).includes('AFTER RELOAD')), JSON.stringify(events));
  await first.unregisterTool('Lookup');
  await first.clearContext();
  await turn(first, 'Check available tools');
  assert.ok(!f.requests.at(-1).tools.some(tool => tool.function.name === 'Lookup'));
});

test('CLI audio attachments resolve to bytes and survive session persistence', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'audio_in';
  await writeFile(join(f.cwd, 'sound.wav'), Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WAVE'), Buffer.alloc(20)]));
  const result = await f.run(['--trust', '-p', 'Listen', '--attach', 'sound.wav']);
  assert.equal(result.code, 0, result.stderr);
  assert.match(JSON.stringify(f.requests.at(-1)), /data:audio\/wav;base64,/);
  const id = result.stderr.match(/Session: (\S+)/)?.[1];
  assert.ok(id, result.stderr);
  const resumed = await f.run(['--resume', id, '-p', 'Listen again']);
  assert.equal(resumed.code, 0, resumed.stderr);
  assert.match(JSON.stringify(f.requests.at(-1)), /data:audio\/wav;base64,/);
  f.server.removeAllListeners('request');
  f.server.on('request', async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); f.requests.push(body);
    const call = !body.messages.some(message => message.role === 'tool');
    const delta = call ? { tool_calls: [{ index: 0, id: 'audio-read', type: 'function', function: { name: 'ReadMediaFile', arguments: '{"path":"sound.wav"}' } }] } : { content: 'Read audio.' };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end(`data: ${JSON.stringify({ id: 'audio', choices: [{ index: 0, delta, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ id: 'audio', choices: [{ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }] })}\n\ndata: [DONE]\n\n`);
  });
  const readAudio = await f.run(['--yolo', '--json', '-p', 'Read sound.wav']);
  assert.equal(readAudio.code, 0, readAudio.stderr);
  assert.match(readAudio.stdout, /audio_url/);
  assert.match(JSON.stringify(f.requests.at(-1)), /data:audio\/wav;base64,/);
  f.env.CODERAI_MODEL_CAPABILITIES = 'none';
  const unsupported = await f.run(['-p', 'Listen', '--attach', 'sound.wav']);
  assert.equal(unsupported.code, 1);
  assert.match(unsupported.stderr, /does not support audio input/);
});

test('WebSearch works with a custom API-key endpoint and reports its status in doctor', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_SEARCH_BASE_URL = `${f.env.CODERAI_MODEL_BASE_URL.replace('/v1', '')}/search`;
  f.env.CODERAI_SEARCH_API_KEY = 'search-test-secret';
  const searchRequests = [];
  f.server.removeAllListeners('request');
  f.server.on('request', async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    if (req.url === '/search') {
      searchRequests.push({ body, authorization: req.headers.authorization });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ search_results: [{ title: 'Local result', url: 'https://example.test/result', snippet: 'Search worked.' }] }));
      return;
    }
    f.requests.push(body);
    const call = !body.messages.some(message => message.role === 'tool');
    const delta = call ? { tool_calls: [{ index: 0, id: 'search-1', type: 'function', function: { name: 'WebSearch', arguments: '{"query":"local test"}' } }] } : { content: 'Found local result.' };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end(`data: ${JSON.stringify({ id: 'search', choices: [{ index: 0, delta, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ id: 'search', choices: [{ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }] })}\n\ndata: [DONE]\n\n`);
  });
  let result = await f.run(['doctor']);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /Web search: configured/);
  result = await f.run(['--trust', '--yolo', '--json', '-p', 'Search for a local test']);
  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(searchRequests, [{ body: { text_query: 'local test' }, authorization: 'Bearer search-test-secret' }]);
  assert.ok(f.requests[0].tools.some(tool => tool.function.name === 'WebSearch'));
  assert.match(result.stdout, /Local result/);
  assert.doesNotMatch(result.stdout + result.stderr, /search-test-secret/);
  // The same endpoint works from the saved TOML service domain without env.
  delete f.env.CODERAI_SEARCH_BASE_URL; delete f.env.CODERAI_SEARCH_API_KEY;
  await writeFile(join(f.home, 'config.toml'), `[services.moonshot_search]\nbase_url = ${JSON.stringify(`${f.env.CODERAI_MODEL_BASE_URL.replace('/v1', '')}/search`)}\napi_key = "saved-search-key"\n`);
  result = await f.run(['--yolo', '--json', '-p', 'Search using the saved endpoint']);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(searchRequests.at(-1).authorization, 'Bearer saved-search-key');
});
