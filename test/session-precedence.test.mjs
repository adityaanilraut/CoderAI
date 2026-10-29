import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, access } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

const events = result => result.stdout.trim().split('\n').map(line => JSON.parse(line));

test('resume uses its saved model and permission; explicit overrides win', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const base = f.env.CODERAI_MODEL_BASE_URL;
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  f.env.TEST_PROVIDER_KEY = 'test-only-key';
  const config = defaultModel => `default_model = "${defaultModel}"\ntelemetry = false\n[providers.fixture]\ntype = "openai"\nbase_url = "${base}"\napi_key_env = "TEST_PROVIDER_KEY"\n[models.coding]\nprovider = "fixture"\nmodel = "fixture-model"\nmax_context_size = 262144\ncapabilities = []\n`;
  await writeFile(join(f.home, 'config.toml'), config('coding'));
  const created = await f.run(['--trust', '--yolo', '-p', 'hello', '--json']);
  assert.equal(created.code, 0, created.stderr);
  const id = events(created)[0].sessionId;

  await writeFile(join(f.home, 'config.toml'), config('missing'));
  const resumed = await f.run(['--resume', id, '-p', 'SCENARIO deny', '--json']);
  assert.equal(resumed.code, 0, resumed.stderr);
  await access(join(f.cwd, 'denied.txt'));
  assert.equal(events(resumed)[0].sessionId, id);
  const continued = await f.run(['--continue', '-p', 'hello', '--json']);
  assert.equal(continued.code, 0, continued.stderr);
  assert.equal(events(continued)[0].sessionId, id);
  const explicit = await f.run(['--resume', id, '--model', 'coding', '-p', 'hello']);
  assert.equal(explicit.code, 0, explicit.stderr);
  const invalid = await f.run(['--resume', id, '--model', 'missing', '-p', 'hello']);
  assert.equal(invalid.code, 1);
  assert.match(invalid.stderr, /Unknown model alias/);
});

test('new sessions save their effective permission mode across config changes', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const config = join(f.home, 'config.toml');
  const created = await f.run(['--trust', '-p', 'hello', '--json']);
  assert.equal(created.code, 0, created.stderr);
  const id = events(created)[0].sessionId;
  await writeFile(config, 'telemetry = false\ndefault_permission_mode = "yolo"\n');
  const resumed = await f.run(['--resume', id, '-p', 'SCENARIO deny', '--json']);
  assert.equal(resumed.code, 1, resumed.stderr);
  assert.ok(events(resumed).some(event => event.type === 'coderai.approval'));
  await assert.rejects(access(join(f.cwd, 'denied.txt')));
  const fresh = await f.run(['-p', 'SCENARIO deny', '--json']);
  assert.equal(fresh.code, 0, fresh.stderr);
  await access(join(f.cwd, 'denied.txt'));
});
