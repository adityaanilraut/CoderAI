import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { test } from 'node:test';
import { parse } from 'smol-toml';
import { fixture } from './fixture.mjs';

test('a registry key stays scoped to catalog requests and refresh respects provider ownership', { timeout: 45000 }, async t => {
  const f = await fixture();
  t.after(f.cleanup);
  const inference = f.env.CODERAI_MODEL_BASE_URL;
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  delete f.env.OPENAI_API_KEY;
  f.env.FIXTURE_REGISTRY_KEY = 'catalog-fixture-token';
  f.env.FIXTURE_INFERENCE_KEY = 'inference-fixture-token';
  const configPath = join(f.home, 'config.toml');
  await writeFile(configPath, [
    'default_model = "manual"',
    '[providers.manual]',
    'type = "openai"',
    `base_url = "${inference}"`,
    'api_key_env = "FIXTURE_INFERENCE_KEY"',
    '[models.manual]',
    'model = "manual-fixture"',
    'provider = "manual"',
    'max_context_size = 131072',
    '',
  ].join('\n'));
  const entry = (id, api) => ({ id, name: id, api, type: 'openai', models: { fixture: { id: 'fixture' } } });
  let catalog = { registered: entry('registered', inference) };
  const catalogAuth = [];
  const server = createServer((req, res) => {
    catalogAuth.push(req.headers.authorization);
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify(catalog));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {
    server.closeAllConnections();
    return new Promise(resolve => server.close(resolve));
  });
  const url = `http://127.0.0.1:${server.address().port}/api.json`;
  const imported = await f.run(['providers', 'import', url, 'FIXTURE_REGISTRY_KEY']);
  assert.equal(imported.code, 0, imported.stdout + imported.stderr);
  assert.deepEqual(catalogAuth, ['Bearer catalog-fixture-token']);
  const saved = await readFile(configPath, 'utf8');
  assert.doesNotMatch(saved + imported.stdout + imported.stderr, /catalog-fixture-token/);
  const initial = parse(saved);
  assert.equal(initial.providers.registered.source.apiKeyEnv, 'FIXTURE_REGISTRY_KEY');
  assert.equal(initial.providers.registered.api_key, undefined);
  assert.equal(initial.providers.registered.api_key_env, undefined);
  assert.equal(initial.default_model, 'manual');

  const missing = await f.run(['--trust', '-m', 'registered/fixture', '-p', 'Check registry credentials']);
  assert.equal(missing.code, 1, missing.stderr);
  assert.equal(f.requests.length, 0);
  const rebound = await f.run(['providers', 'update', 'registered', 'openai', inference, 'FIXTURE_INFERENCE_KEY']);
  assert.equal(rebound.code, 0, rebound.stdout + rebound.stderr);
  const ready = await f.run(['-m', 'registered/fixture', '-p', 'Use the separate inference credential']);
  assert.equal(ready.code, 0, ready.stderr);

  catalog = {
    registered: entry('registered', `${inference}/new-endpoint`),
    manual: entry('manual', `${inference}/alternate`),
  };
  const refresh = await f.run(['providers', 'refresh']);
  assert.equal(refresh.code, 1, refresh.stdout + refresh.stderr);
  assert.match(refresh.stderr, /manual/);
  assert.match(refresh.stderr, /registered/);
  const final = parse(await readFile(configPath, 'utf8'));
  assert.equal(final.providers.manual.base_url, inference);
  assert.equal(final.providers.registered.base_url, inference);
  assert.equal(final.providers.registered.api_key_env, 'FIXTURE_INFERENCE_KEY');
  assert.equal(final.models.manual.model, 'manual-fixture');
  assert.equal(final.default_model, 'manual');
});
