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
