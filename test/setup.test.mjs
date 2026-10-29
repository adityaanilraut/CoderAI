import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { register } from 'tsx/esm/api';
import { fixture as cliFixture, cli } from './fixture.mjs';
register();
const { runSetup } = await import('../apps/coderai/src/setup.ts');
const { SetupCancelled } = await import('../apps/coderai/src/setup-terminal.ts');

function fixture(answers, initial = {}) {
  const config = { providers: {}, models: {}, ...initial };
  const questions = [];
  const runtime = { harness: {
    configPath: '/test/config.toml',
    getConfig: async () => config,
    setConfig: async patch => {
      if (patch.providers) Object.assign(config.providers, patch.providers);
      if (patch.models) Object.assign(config.models, patch.models);
      if (patch.defaultModel) config.defaultModel = patch.defaultModel;
    },
  } };
  return { config, runtime, questions, prompt: async (question, secret) => {
    questions.push({ question, secret });
    if (!answers.length) throw Object.assign(new Error(`unexpected prompt: ${question}`), { name: 'AbortError' });
    const answer = answers.shift();
    if (answer instanceof Error) throw answer;
    return answer;
  } };
}
const noDiscovery = async () => { throw new Error('Discovery should not run for manual entry'); };

test('setup guides provider creation through manual model selection and a default', async () => {
  const f = fixture(['1', '1', '', '', '2', '', '1', '2', 'example-model', 'coding', '100k', '4', '1', '', '0']);
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.equal(f.config.providers.openai.apiKeyEnv, 'OPENAI_API_KEY');
  assert.equal(f.config.providers.openai.baseUrl, 'https://api.openai.com/v1');
  assert.deepEqual(f.config.models.coding, { provider: 'openai', model: 'example-model', maxContextSize: 100000, capabilities: ['thinking'] });
  assert.equal(f.config.defaultModel, 'coding');
});

test('setup validates fields in place and keeps prior entries', async () => {
  const f = fixture(['1', '3', 'bad id', '', 'bad url', '', '2', 'not a variable', '', '2', '0']);
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.deepEqual(Object.keys(f.config.providers), ['deepseek']);
  assert.equal(f.config.providers.deepseek.apiKeyEnv, 'DEEPSEEK_API_KEY');
});

test('setup accepts a hidden API key and pasted Markdown URL', async () => {
  const f = fixture(['1', '3', '', '[https://api.deepseek.com](https://api.deepseek.com)', '1', '', 'example-secret-key', '2', '0']);
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.equal(f.config.providers.deepseek.apiKey, 'example-secret-key');
  assert.equal(f.config.providers.deepseek.baseUrl, 'https://api.deepseek.com');
  assert.equal(f.config.providers.deepseek.apiKeyEnv, undefined);
  assert.ok(f.questions.filter(q => q.question.includes('API key (input hidden)')).every(q => q.secret));
});

test('default picker offers a model-less provider and saves a discovered model as default', async () => {
  const f = fixture(['3', '2', '1', '1', '', '', '1', '0'], {
    providers: { openai: { type: 'openai' }, deepseek: { type: 'openai', apiKey: 'test-key' } },
    models: { existing: { provider: 'openai', model: 'existing', maxContextSize: 10000 } }, defaultModel: 'existing',
  });
  let discovered = false;
  await runSetup(f.runtime, f.prompt, async (provider, signal) => {
    assert.equal(provider.apiKey, 'test-key'); assert.ok(signal instanceof AbortSignal); discovered = true;
    return [{ id: 'discovered-model', context: 128000, capabilities: ['thinking'] }];
  });
  assert.ok(discovered);
  assert.equal(f.config.defaultModel, 'discovered-model');
  assert.deepEqual(f.config.models['discovered-model'], { provider: 'deepseek', model: 'discovered-model', maxContextSize: 128000, capabilities: ['thinking'] });
  assert.ok(f.config.models.existing);
});

test('discovery failure and empty lists allow manual entry without losing the provider', async () => {
  for (const discovery of [async () => { throw new Error('Model discovery returned HTTP 401. Check the API key.'); }, async () => []]) {
    const f = fixture(['2', '1', '1', '2', 'org/new.model', '', '0', '128,000', '2', '2', '3', '1', '2', '0'], {
      providers: { custom: { type: 'openai' } },
    });
    await runSetup(f.runtime, f.prompt, discovery);
    assert.deepEqual(f.config.models['org-new-model'], { provider: 'custom', model: 'org/new.model', maxContextSize: 128000, capabilities: ['video_in'] });
    assert.equal(f.config.defaultModel, undefined);
  }
});

test('setup suggests unique aliases, rejects duplicates, and preserves the current default', async () => {
  const f = fixture(['2', '1', '2', 'model', 'model', '', '1m', '1', '', '0'], {
    providers: { custom: { type: 'openai' } },
    models: { model: { provider: 'custom', model: 'old', maxContextSize: 10000 } }, defaultModel: 'model',
  });
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.equal(f.config.models.model.model, 'old');
  assert.equal(f.config.models['model-2'].model, 'model');
  assert.equal(f.config.defaultModel, 'model');
});

test('Esc cancels an unfinished step and AbortError exits the setup loop', async () => {
  const f = fixture(['1', '3', new SetupCancelled(), '0']);
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.deepEqual(f.config.providers, {});
  const aborted = fixture([Object.assign(new Error('cancelled'), { name: 'AbortError' })]);
  await assert.rejects(runSetup(aborted.runtime, aborted.prompt), { name: 'AbortError' });
});

test('custom providers use a protocol selector', async () => {
  const f = fixture(['1', '6', 'custom', '3', 'https://example.com', '2', 'CUSTOM_API_KEY', '0', '0']);
  await runSetup(f.runtime, f.prompt, noDiscovery);
  assert.equal(f.config.providers.custom.type, 'anthropic');
});

const terminal = (f, actions) => new Promise((done, reject) => {
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = ''; let err = ''; child.stdout.on('data', c => out += c); child.stderr.on('data', c => err += c);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: process.execPath, cli, env: { ...f.env, TERM: 'xterm-256color' }, actions, args: ['--setup'] }));
  child.on('error', reject); child.on('close', code => code === 0 ? done(JSON.parse(out)) : reject(new Error(err + out)));
});

test('real PTY: setup supports arrows, search, hidden keys, immediate model setup and default selection', { timeout: 40000 }, async t => {
  const f = await cliFixture(); t.after(f.cleanup);
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  const result = await terminal(f, [
    { wait: 'Ctrl+C exit', send: '\r' },
    { wait: 'Add provider · choose a service', send: 'deepseek\r' },
    { wait: 'Provider ID', send: '\r' },
    { wait: 'API base URL', send: '\r' },
    { wait: 'API key · choose how to save it', send: '\r' },
    { wait: 'API key (input hidden)', send: 'PTY_SECRET_VALUE\r' },
    { wait: 'Provider deepseek is ready', send: '\r' },
    { wait: 'Add model · deepseek', send: '\u001b[B\r' },
    { wait: 'Model ID from your provider', send: 'test-model\r' },
    { wait: 'Model alias', send: '\r' },
    { wait: 'Context limit in tokens', send: '128k\r' },
    { wait: 'Model capabilities', send: '\r' },
    { wait: 'Use this model by default?', send: '\r' },
    { wait: 'Default model: test-model', send: '\u001b' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.doesNotMatch(result.output, /PTY_SECRET_VALUE/);
  const config = await readFile(join(f.home, 'config.toml'), 'utf8');
  assert.match(config, /test-model/); assert.match(config, /128000/);
  assert.match(config, /PTY_SECRET_VALUE/);
});

test('real PTY: field cancellation and Ctrl+C restore the terminal', { timeout: 25000 }, async t => {
  const f = await cliFixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Ctrl+C exit', send: '\r' },
    { wait: 'Add provider · choose a service', send: '\r' },
    { wait: 'Provider ID', send: '\u001b' },
    { wait: 'Step cancelled.', send: '\u0003' },
  ]);
  assert.equal(result.code, 130, result.output);
  assert.ok(result.terminalRestored);
});


test('real PTY: an existing provider with no models can become the default using live discovery', { timeout: 30000 }, async t => {
  const f = await cliFixture(); t.after(f.cleanup);
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  const requests = [];
  const server = createServer((req, res) => {
    requests.push({ url: req.url, key: req.headers.authorization });
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ data: Array.from({ length: 15 }, (_, index) => ({
      id: `available-${String(index + 1).padStart(2, '0')}`, context_window: 128000,
    })) }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  await writeFile(join(f.home, 'config.toml'), `
telemetry = false
default_model = "existing"
[providers.openai]
type = "openai"
base_url = "http://127.0.0.1:1"
api_key = "existing-key"
[providers.deepseek]
type = "openai"
base_url = "http://127.0.0.1:${server.address().port}"
api_key = "discovery-key"
[models.existing]
provider = "openai"
model = "existing"
max_context_size = 100000
`);
  const result = await terminal(f, [
    { wait: 'Ctrl+C exit', send: '3\r' },
    { wait: 'deepseek · add a model', send: '\u001b[B\r' },
    { wait: 'Add model · deepseek', send: '\r' },
    { wait: 'Available models · deepseek', send: '10\r' },
    { wait: 'Model alias', send: '\r' },
    { wait: 'Context limit in tokens', send: '\r' },
    { wait: 'Model capabilities', send: '\r' },
    { wait: 'Default model: available-10', send: '\u001b' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.deepEqual(requests, [{ url: '/models', key: 'Bearer discovery-key' }]);
  assert.doesNotMatch(result.output, /discovery-key|existing-key/);
  const config = await readFile(join(f.home, 'config.toml'), 'utf8');
  assert.match(config, /default_model = "available-10"/);
  assert.match(config, /\[models\.existing\]/);
  assert.match(config, /\[models\.available-10\]/);
});

test('real PTY: SIGTERM during hidden key entry restores input without saving the unfinished provider', { timeout: 20000 }, async t => {
  const f = await cliFixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Ctrl+C exit', send: '\r' },
    { wait: 'Add provider · choose a service', send: '\r' },
    { wait: 'Provider ID', send: '\r' },
    { wait: 'API base URL', send: '\r' },
    { wait: 'API key · choose how to save it', send: '\r' },
    { wait: 'API key (input hidden)', signal: 'SIGTERM' },
  ]);
  assert.equal(result.code, 130, result.output); assert.ok(result.terminalRestored);
});
