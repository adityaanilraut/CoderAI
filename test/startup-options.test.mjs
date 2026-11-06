import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('startup options expose additional directory access and validate agent selection', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.root, 'outside.txt'), 'extra directory marker');
  const allowed = await f.run(['--trust', '--add-dir', f.root, '-p', 'SCENARIO outside', '--json']);
  assert.equal(allowed.code, 0, allowed.stderr);
  const events = allowed.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.ok(!events.some(event => event.type === 'coderai.approval'));
  const unknown = await f.run(['--agent', 'nonexistent-test-agent', '-p', 'hello']);
  assert.equal(unknown.code, 1);
  assert.match(unknown.stderr, /agent|profile/i);
});

test('CLI help and startup errors give usable commands without opening a session', { timeout: 15000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const help = await f.run(['--help']);
  assert.equal(help.code, 0, help.stderr);
  assert.match(help.stdout, /coderai sessions list --json/);
  assert.ok(!help.stdout.includes(f.cwd), help.stdout);
  const models = await f.run(['models', '--help']);
  assert.match(models.stdout, /set the default model for new conversations/);
  const empty = await f.run(['--json', '-p', ' \t '], '\n');
  assert.equal(empty.code, 1);
  assert.equal(empty.stdout, '');
  assert.match(empty.stderr, /Use -p TEXT or pipe text/);
  const missing = await f.run(['-C', join(f.root, 'missing'), '-p', 'hello']);
  assert.equal(missing.code, 1);
  assert.match(missing.stderr, /Workspace directory does not exist: .*missing/);
  const file = join(f.cwd, 'input.txt');
  const invalid = await f.run(['-C', file, '-p', 'hello']);
  assert.equal(invalid.code, 1);
  assert.match(invalid.stderr, /Workspace path is not a directory: .*input\.txt/);
  assert.equal(f.requests.length, 0);
});
