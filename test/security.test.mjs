import { test } from 'node:test';
import assert from 'node:assert/strict';
import { symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';
import { execFileSync } from 'node:child_process';

test('workspace trust is explicit and persists across invocations', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.KIMI_CODE_TRUST_WORKSPACE = 'true';
  const untrusted = await f.run(['-p', 'hello']);
  assert.equal(untrusted.code, 1, untrusted.stderr);
  assert.match(untrusted.stderr, /Workspace is not trusted/);
  assert.equal(f.requests.length, 0);
  const trusted = await f.run(['--trust', '-p', 'hello']);
  assert.equal(trusted.code, 0, trusted.stderr);
  assert.ok(JSON.stringify(f.requests.at(-1)).includes('TEST_INSTRUCTION_42'));
  const saved = await f.run(['-p', 'hello']);
  assert.equal(saved.code, 0, saved.stderr);
});

test('manual permissions allow eligible writes in a Git worktree as Kimi does', { timeout: 30000, skip: process.platform === 'win32' }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  execFileSync('git', ['init', '-q', f.cwd]);
  const result = await f.run(['--trust', '--json', '-p', 'SCENARIO deny']);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await (await import('node:fs/promises')).readFile(join(f.cwd, 'denied.txt'), 'utf8'), 'should not exist');
  assert.ok(!result.stdout.split('\n').filter(Boolean).map(JSON.parse).some(event => event.type === 'coderai.approval'));
});

test('manual permissions match Kimi default reads through a symlink outside the workspace', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.root, 'outside.txt'), 'private symlink content');
  await symlink(join(f.root, 'outside.txt'), join(f.cwd, 'outside-link.txt'));
  const result = await f.run(['--trust', '--json', '-p', 'SCENARIO symlink']);
  assert.equal(result.code, 0, result.stderr);
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.ok(!events.some(event => event.type === 'coderai.approval'));
  assert.ok(JSON.stringify(f.requests).includes('private symlink content'));
});
