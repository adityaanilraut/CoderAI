import { test } from 'node:test';
import assert from 'node:assert/strict';
import { copyFile, chmod, mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fixture } from './fixture.mjs';

const builtBinary = resolve(process.env.CODERAI_TEST_BINARY || join('dist-bun', `${process.platform}-${process.arch}`, process.platform === 'win32' ? 'coderai.exe' : 'coderai'));
const standaloneRequested = Boolean(process.env.CODERAI_TEST_BINARY);
if (standaloneRequested && !existsSync(builtBinary)) throw new Error(`Standalone executable not found: ${builtBinary}`);
test('standalone extracts its native clipboard binding and handles pasted media', { timeout: 30000, skip: !standaloneRequested || process.platform !== 'darwin' }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const dir = join(f.root, 'tmp'); const bin = join(f.root, 'bin'); await mkdir(dir); await mkdir(bin);
  const file = join(f.cwd, 'pixel.png');
  await writeFile(file, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64'));
  const marker = join(f.root, 'binding-loaded');
  const bindingDir = 'coderai-bun-*';
  await writeFile(join(bin, 'osascript'), `#!/bin/sh\nls "$TMPDIR"/${bindingDir}/clipboard.node > "$CODERAI_BINDING_MARKER"\nprintf "%s" "$CODERAI_CLIPBOARD_RESULT"\n`, { mode: 0o755 });
  Object.assign(f.env, { PATH: `${bin}:${process.env.PATH}`, TMPDIR: dir, CODERAI_MODEL_CAPABILITIES: 'image_in', CODERAI_BINDING_MARKER: marker, CODERAI_CLIPBOARD_RESULT: JSON.stringify({ path: file }) });
  const result = await new Promise((resolveResult, reject) => {
    const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
    let out = ''; let err = ''; child.stdout.on('data', c => out += c); child.stderr.on('data', c => err += c);
    child.on('error', reject); child.on('close', code => code === 0 ? resolveResult(JSON.parse(out)) : reject(new Error(err + out)));
    child.stdin.end(JSON.stringify({ cwd: f.cwd, executable: builtBinary, env: { ...f.env, TERM: 'xterm-256color' }, actions: [
      { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: '\u0016' },
      { wait: 'Clipboard media staged', send: 'Describe image\r' },
      { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
    ] }));
  });
  assert.equal(result.code, 0, result.output);
  assert.match(await readFile(marker, 'utf8'), /clipboard\.node/);
  assert.match(JSON.stringify(f.requests[0]), /data:image\/png;base64,/);
});
test('standalone executable runs the engine and tools outside the checkout', { timeout: 60000, skip: !standaloneRequested && 'run bun run test:native to build and test the Bun executable' }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'coderai-native-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const binary = join(root, process.platform === 'win32' ? 'coderai.exe' : 'coderai');
  await copyFile(builtBinary, binary);
  if (process.platform !== 'win32') await chmod(binary, 0o755);
  const f = await fixture(); t.after(f.cleanup);
  const result = await new Promise((resolveRun, reject) => {
    const child = spawn(binary, ['--trust', '--yolo', '--json', '-p', 'SCENARIO tools'], { cwd: f.cwd, env: f.env });
    let stdout = '', stderr = '';
    child.stdout.on('data', data => stdout += data); child.stderr.on('data', data => stderr += data);
    child.on('error', reject); child.on('close', (code, signal) => resolveRun({ code, signal, stdout, stderr }));
    child.stdin.end();
  });
  assert.equal(result.code, 0, result.stderr || `signal: ${result.signal}`);
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).exitCode, 0, result.stderr);
  assert.equal(events.filter(event => event.type === 'tool.result').length, 7);
  assert.equal(await readFile(join(f.cwd, 'created.txt'), 'utf8'), 'second\n');
});

test('standalone embeds a working MiniDb text worker', { timeout: 60000, skip: !standaloneRequested }, async t => {
  const f = await fixture();
  const temp = join(f.root, 'tmp'); await mkdir(temp);
  const child = spawn(builtBinary, ['--trust', '--json', '-p', 'SCENARIO wait'], { cwd: f.cwd, env: { ...f.env, TMPDIR: temp, TMP: temp, TEMP: temp }, stdio: ['ignore', 'ignore', 'pipe'] });
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) {
      const closed = new Promise(resolve => child.once('close', resolve));
      child.kill();
      await closed;
    }
    await f.cleanup();
  });
  const { readdir } = await import('node:fs/promises');
  let worker;
  for (let i = 0; i < 100 && !worker; i++) {
    const dirs = await readdir(temp);
    const dir = dirs.find(name => name.startsWith('coderai-bun-'));
    if (dir && existsSync(join(temp, dir, 'text-build-worker.mjs'))) worker = join(temp, dir, 'text-build-worker.mjs');
    else await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.ok(worker, 'standalone did not extract its worker');
  const extracted = join(f.root, 'worker.mjs'); await copyFile(worker, extracted);
  const entry = join(f.root, 'worker-test.ts');
  await writeFile(entry, `
    import worker from './worker.mjs' with { type: 'file' };
    import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
    import { tmpdir } from 'node:os';
    import { join } from 'node:path';
    import { MiniDb } from ${JSON.stringify(resolve('packages/minidb/src/index.ts'))};
    import { configureTextBuildWorkerRuntime } from ${JSON.stringify(resolve('packages/minidb/src/worker-runtime.ts'))};
    const dir = mkdtempSync(join(tmpdir(), 'coderai-worker-test-'));
    const path = join(dir, 'worker.mjs');
    writeFileSync(path, new Uint8Array(await Bun.file(worker).arrayBuffer()));
    configureTextBuildWorkerRuntime(path);
    const db = await MiniDb.open({ dir: join(dir, 'db'), durability: 'relaxed', valueCodec: 'json' });
    try {
      await db.mset(Array.from({ length: 5000 }, (_, i) => ['doc' + i, { text: 'searchable document ' + i }]));
      await db.createTextIndex('text', { fields: ['text'] });
      const hits = db.search('text', 'searchable', { limit: 10 });
      if (hits.length !== 10 || hits.some(hit => !hit.value?.text?.includes('searchable'))) throw new Error('worker index search failed');
      console.log(JSON.stringify(db.stats));
    } finally { await db.close(); rmSync(dir, { recursive: true, force: true }); }
  `);
  const { execFile } = await import('node:child_process');
  const { promisify } = await import('node:util');
  const exec = promisify(execFile);
  const harness = join(f.root, process.platform === 'win32' ? 'worker-test.exe' : 'worker-test');
  await exec(process.execPath, ['build', '--compile', entry, '--outfile', harness], { timeout: 30000 });
  const result = await exec(harness, [], { timeout: 30000 });
  const stats = JSON.parse(result.stdout.trim());
  assert.ok(stats.textWorkerBuilds > 0, result.stdout);
  assert.equal(stats.textWorkerFallbacks, 0);
});
