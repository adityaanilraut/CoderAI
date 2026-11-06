import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chmod, readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('provider and model commands preserve existing configuration and never expose keys', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const base = f.env.CODERAI_MODEL_BASE_URL;
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  f.env.TEST_KEY_A = 'secret-value-a'; f.env.TEST_KEY_B = 'secret-value-b';
  const run = async (...args) => {
    const result = await f.run(args);
    assert.equal(result.code, 0, `${args.join(' ')}: ${result.stderr}`);
    assert.doesNotMatch(result.stdout + result.stderr, /secret-value-/);
    return result;
  };
  await run('providers', 'add', 'first', 'openai', base, 'TEST_KEY_A');
  await run('models', 'add', 'first-model', 'first', 'one', '100000', 'image_in');
  await run('models', 'default', 'first-model');
  await run('providers', 'add', 'second', 'openai', base, 'TEST_KEY_B');
  await run('models', 'add', 'second-model', 'second', 'two', '100000', 'thinking');
  await run('models', 'secondary', 'second-model');
  await run('permission-default', 'manual');
  const providers = await run('providers', 'list');
  assert.match(providers.stdout, /first\s+openai/); assert.match(providers.stdout, /second\s+openai/);
  const models = await run('models', 'list');
  assert.match(models.stdout, /first-model\s+first\s+one\s+default/);
  assert.match(models.stdout, /second-model\s+second\s+two/);
  const config = await readFile(join(f.home, 'config.toml'), 'utf8');
  assert.match(config, /first-model/); assert.match(config, /second-model/);
  assert.doesNotMatch(config, /secret-value-/);
  await run('doctor');
  const duplicate = await f.run(['providers', 'add', 'first', 'openai', base, 'TEST_KEY_A']);
  assert.equal(duplicate.code, 1); assert.match(duplicate.stderr, /already exists/);
});

test('saving configuration keeps credentials private after an atomic rewrite', { timeout: 20000, skip: process.platform === 'win32' }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const path = join(f.home, 'config.toml');
  const first = await f.run(['permission-default', 'manual']);
  assert.equal(first.code, 0, first.stderr);
  await chmod(path, 0o600);
  const second = await f.run(['providers', 'add', 'fixture', 'openai', f.env.CODERAI_MODEL_BASE_URL, 'TEST_KEY']);
  assert.equal(second.code, 0, second.stderr);
  assert.equal((await stat(path)).mode & 0o777, 0o600);
});

test('provider and model updates preserve aliases and defaults', async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  for (const args of [
    ['providers', 'add', 'example', 'openai', 'https://example.test/v1', 'OLD_KEY'],
    ['models', 'add', 'coding', 'example', 'old-model', '100000', 'image_in'],
    ['models', 'default', 'coding'],
    ['providers', 'update', 'example', 'openai', 'https://new.example.test/v1', 'NEW_KEY'],
    ['models', 'update', 'coding', 'example', 'new-model', '200000'],
  ]) { const result = await f.run(args); assert.equal(result.code, 0, result.stderr); }
  const config = await readFile(join(f.home, 'config.toml'), 'utf8');
  assert.match(config, /NEW_KEY/); assert.doesNotMatch(config, /OLD_KEY/);
  assert.match(config, /new-model/); assert.match(config, /image_in/);
  assert.match(config, /default_model = "coding"/);
});
