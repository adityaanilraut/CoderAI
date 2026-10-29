import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('selected agent profile changes tools and is verified on resume', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const selected = await f.run(['--trust', '--agent', 'explore', '-p', 'hello', '--json']);
  assert.equal(selected.code, 0, selected.stderr);
  const id = JSON.parse(selected.stdout.split('\n')[0]).sessionId;
  const names = (f.requests.at(-1).tools ?? []).map(tool => tool.function?.name);
  assert.ok(names.includes('Read'));
  assert.ok(!names.includes('Edit'));
  const resumed = await f.run(['--resume', id, '--agent', 'explore', '-p', 'hello']);
  assert.equal(resumed.code, 0, resumed.stderr);
  const mismatch = await f.run(['--resume', id, '--agent', 'coder', '-p', 'hello']);
  assert.equal(mismatch.code, 1); assert.match(mismatch.stderr, /uses explore/);
  await writeFile(join(f.cwd, 'custom.md'), '---\nname: custom-agent\ndescription: Custom fixture agent\ntools:\n  - Read\n---\nCUSTOM_AGENT_PROMPT_MARKER\n');
  const custom = await f.run(['--agent-file', 'custom.md', '-p', 'hello']);
  assert.equal(custom.code, 0, custom.stderr);
  assert.match(JSON.stringify(f.requests.at(-1)), /CUSTOM_AGENT_PROMPT_MARKER/);
  const unknown = await f.run(['--agent-file', 'missing.md', '-p', 'hello']);
  assert.equal(unknown.code, 1); assert.match(unknown.stderr, /missing\.md/);
});
