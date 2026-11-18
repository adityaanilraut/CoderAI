import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('CLI imports, forks and exports local sessions without modifying sources', { timeout: 60000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const sources = {
    codex: [
      { type: 'session_meta', payload: { id: 'example-session', cwd: f.cwd, source: 'cli' } },
      { type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: 'Codex source prompt' }] } },
      { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Codex source answer' }] } },
    ],
    'claude-code': [
      { type: 'user', uuid: 'u', parentUuid: null, sessionId: 'claude-session', cwd: f.cwd, message: { role: 'user', content: 'Claude source prompt' } },
      { type: 'assistant', uuid: 'a', parentUuid: 'u', sessionId: 'claude-session', message: { role: 'assistant', content: [{ type: 'text', text: 'Claude source answer' }] } },
    ],
  };
  for (const [source, rows] of Object.entries(sources)) {
    const path = join(f.root, `${source}.jsonl`);
    const original = rows.map(row => JSON.stringify(row)).join('\n') + '\n';
    await writeFile(path, original);
    const imported = await f.run(['--trust', 'sessions', 'import', source, path]);
    assert.equal(imported.code, 0, imported.stderr);
    const [id, status] = imported.stdout.trim().split('\t');
    assert.ok(id); assert.match(status, /^(complete|partial)$/);
    assert.equal(await readFile(path, 'utf8'), original);
    const output = join(f.cwd, `${source}.md`);
    const exported = await f.run(['sessions', 'export', id, output]);
    assert.equal(exported.code, 0, exported.stderr);
    const text = await readFile(output, 'utf8');
    assert.match(text, new RegExp(`${source === 'codex' ? 'Codex' : 'Claude'} source prompt`));
    assert.match(text, new RegExp(`${source === 'codex' ? 'Codex' : 'Claude'} source answer`));
    const repeated = await f.run(['sessions', 'export', id, output]);
    assert.equal(repeated.code, 1); assert.match(repeated.stderr, /EEXIST/);
    const forked = await f.run(['sessions', 'fork', id]);
    assert.equal(forked.code, 0, forked.stderr);
    const forkId = forked.stdout.trim(); assert.ok(forkId); assert.notEqual(forkId, id);
    const forkPath = join(f.cwd, `${source}-fork.md`);
    const forkExport = await f.run(['sessions', 'export', forkId, forkPath]);
    assert.equal(forkExport.code, 0, forkExport.stderr);
    assert.match(await readFile(forkPath, 'utf8'), /source prompt/);
  }
  const badPath = join(f.root, 'bad.jsonl'); await writeFile(badPath, 'not json\n');
  const before = await f.run(['sessions']);
  const bad = await f.run(['sessions', 'import', 'codex', badPath]);
  assert.equal(bad.code, 1); assert.match(bad.stderr, /Session import failed/);
  const after = await f.run(['sessions']);
  assert.equal(after.stdout, before.stdout);
  assert.equal(await readFile(badPath, 'utf8'), 'not json\n');
});

test('saved-session export and fork work offline, and ZIP output has exclusive private publication', { timeout: 60000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const project = join(f.root, 'old-workspace');
  await mkdir(project);
  const source = join(f.root, 'old.jsonl');
  const rows = [
    { type: 'session_meta', payload: { id: 'archived-example', cwd: project, source: 'cli' } },
    { type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: 'Offline archive prompt' }] } },
    { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Offline archive answer' }] } },
  ];
  await writeFile(source, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
  const imported = await f.run(['-C', project, '--trust', 'sessions', 'import', 'codex', source]);
  assert.equal(imported.code, 0, imported.stderr);
  const [id] = imported.stdout.trim().split('\t');
  const list = await f.run(['sessions', '--all', '--json']);
  assert.equal(list.code, 0, list.stderr);
  const statePath = join(JSON.parse(list.stdout).find(row => row.id === id).sessionDir, 'state.json');
  const state = JSON.parse(await readFile(statePath, 'utf8'));
  await writeFile(statePath, JSON.stringify({ ...state, archived: true }));
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_')) delete f.env[key];
  const forked = await f.run(['sessions', 'fork', id]);
  assert.equal(forked.code, 0, forked.stderr);
  const forkId = forked.stdout.trim();
  await rm(project, { recursive: true });
  const exported = await f.run(['sessions', 'export', id, 'offline.md']);
  assert.equal(exported.code, 0, exported.stderr);
  assert.match(await readFile(join(f.cwd, 'offline.md'), 'utf8'), /Offline archive answer/);
  const output = join(f.cwd, 'existing.zip');
  await writeFile(output, 'previous output');
  const collision = await f.run(['sessions', 'export', id, '--zip', output]);
  assert.equal(collision.code, 1);
  assert.match(collision.stderr, /EEXIST/);
  assert.equal(await readFile(output, 'utf8'), 'previous output');
  const archivePath = join(f.cwd, 'contested.zip');
  const simultaneous = await Promise.all([id, forkId].map(sessionId => f.run(['sessions', 'export', sessionId, '--zip', archivePath])));
  assert.equal(simultaneous.filter(result => result.code === 0).length, 1, JSON.stringify(simultaneous));
  assert.match(simultaneous.find(result => result.code !== 0).stderr, /EEXIST/);
  assert.equal((await readFile(archivePath)).subarray(0, 2).toString(), 'PK');
  if (process.platform !== 'win32') assert.equal((await stat(archivePath)).mode & 0o777, 0o600);
});
