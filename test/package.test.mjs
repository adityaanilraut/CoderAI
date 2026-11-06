import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, access, mkdtemp, rm, realpath } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { build } from 'esbuild';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { fixture, testRuntime } from './fixture.mjs';
const exec = promisify(execFile);

test('Bun archive installs and runs the real engine without either source checkout', { timeout: 120000 }, async t => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-package-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const packed = await exec('bun', ['pm', 'pack', '--ignore-scripts', '--filename', join(dir, 'coderai.tgz'), '--quiet'], { timeout: 30000 });
  const archive = join(dir, 'coderai.tgz');
  const listing = await exec('tar', ['-tzf', archive]);
  const files = listing.stdout.trim().split('\n').map(file => file.replace(/^package\//, ''));
  for (const name of ['LICENSE', 'NOTICE', 'dist/LICENSE.pi-tui', 'dist/LICENSE.jsquash-webp', 'dist/LICENSE.libwebp', 'dist/text-build-worker.mjs']) assert.ok(files.includes(name), name);
  assert.ok(!files.some(name => name.startsWith('apps/') || name.includes('/src/')));
  await writeFile(join(dir, 'package.json'), JSON.stringify({ private: true, dependencies: { coderai: `file:${archive}` } }));
  await exec('bun', ['install', '--production'], { cwd: dir, timeout: 90000 });
  const executable = join(dir, 'node_modules', '.bin', 'coderai');
  assert.equal(await realpath(executable), await realpath(join(dir, 'node_modules', 'coderai', 'dist', 'coderai.mjs')));
  const f = await fixture(); t.after(f.cleanup);
  const env = { ...f.env, NODE_OPTIONS: `--require=${resolve('test/runtime-guard.cjs')}`, CODERAI_FORBIDDEN_CHECKOUT: process.cwd() };
  const result = await new Promise((resolveRun, reject) => {
    const child = spawn(executable, ['--trust', '--yolo', '--json', '-p', 'SCENARIO tools'], { cwd: f.cwd, env });
    let stdout = ''; let stderr = '';
    child.stdout.on('data', chunk => stdout += chunk);
    child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolveRun({ stdout, stderr }) : reject(new Error(`Packaged CLI exited ${code}: ${stderr}`)));
    child.stdin.end();
  });
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).exitCode, 0, result.stderr);
  assert.equal(events.filter(event => event.type === 'tool.result').length, 7);
  assert.equal(await readFile(join(f.cwd, 'created.txt'), 'utf8'), 'second\n');

  // Exercise the installed entry point in a real terminal as well as headless.
  // The runtime guard makes any accidental dependency on either checkout fail.
  const terminal = await new Promise((resolveRun, reject) => {
    const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = ''; let stderr = '';
    child.stdout.on('data', chunk => stdout += chunk);
    child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) return reject(new Error(`Packaged PTY failed: ${stderr}${stdout}`));
      try { resolveRun(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify({
      cwd: f.cwd, node: testRuntime, cli: executable,
      env: { ...env, TERM: 'xterm-256color' },
      actions: [
        { wait: 'Idle', send: 'hello\r' },
        { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
      ],
    }));
  });
  assert.equal(terminal.code, 0, terminal.output);
  assert.ok(terminal.terminalRestored, terminal.output);
});

test('packaged dependency closure, embedded prompts and licenses', async () => {
  const meta = JSON.parse(await readFile('dist/metafile.json', 'utf8'));
  const inputs = Object.keys(meta.inputs);
  assert.ok(inputs.some(p => p.includes('agentProfileCatalog/system.md')));
  for (const tool of ['read', 'write', 'grep', 'glob', 'bash']) assert.ok(inputs.some(p => p.endsWith(`/os/${tool}/${tool}.md`)));
  assert.ok(inputs.some(p => p.endsWith('/edit/edit.md')));
  assert.ok(!inputs.some(p => /kap-server|acp-server|remote-control|apps\/vscode|dist-web|custom-theme\.md/.test(p)));
  assert.ok(!inputs.some(p => p.includes('/Downloads/')));
  for (const asset of ['coderai.mjs', 'text-build-worker.mjs', 'LICENSE', 'NOTICE', 'LICENSE.pi-tui', 'LICENSE.jsquash-webp', 'LICENSE.libwebp']) await access(join('dist', asset));
  const result = await exec(resolve('dist/coderai.mjs'), ['--help'], { cwd: tmpdir() });
  assert.match(result.stdout, /Usage: coderai/);
});

test('packaged MiniDb worker builds a real text index', { timeout: 30000 }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-worker-'));
  const entry = join(dir, 'test.mjs');
  try {
    const code = `
      import { MiniDb } from ${JSON.stringify(resolve('packages/minidb/src/index.ts'))};
      import { configureTextBuildWorkerRuntime } from ${JSON.stringify(resolve('packages/minidb/src/worker-runtime.ts'))};
      configureTextBuildWorkerRuntime(${JSON.stringify(resolve('dist/text-build-worker.mjs'))});
      const db = await MiniDb.open({ dir: ${JSON.stringify(join(dir, 'db'))}, durability: 'relaxed', valueCodec: 'json' });
      try {
        await db.mset(Array.from({ length: 5000 }, (_, i) => ['doc' + i, { text: 'fixture searchable document ' + i }]));
        await db.createTextIndex('text', { fields: ['text'] });
        const stats = db.stats;
        console.log(JSON.stringify(typeof stats === 'function' ? stats.call(db) : stats));
      } finally { await db.close(); }
    `;
    await build({ stdin: { contents: code, resolveDir: process.cwd(), sourcefile: 'worker-test.ts' }, bundle: true, platform: 'node', format: 'esm', target: 'node24', outfile: entry });
    const result = await exec(testRuntime, [entry], { timeout: 25000 });
    const stats = JSON.parse(result.stdout.trim());
    assert.ok(stats.textWorkerBuilds > 0, result.stdout);
    assert.equal(stats.textWorkerFallbacks, 0);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('CLI fails clearly if its packaged MiniDb worker is missing', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-missing-worker-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const { copyFile, symlink } = await import('node:fs/promises');
  await copyFile(resolve('dist/coderai.mjs'), join(dir, 'coderai.mjs'));
  await symlink(resolve('node_modules'), join(dir, 'node_modules'), 'junction');
  await assert.rejects(exec(process.execPath, [join(dir, 'coderai.mjs'), '--help']), error => {
    assert.match(error.stderr, /MiniDb text-build worker entry is not readable/);
    return true;
  });
});

test('standalone tests reject a missing requested executable', async () => {
  const missing = join(tmpdir(), `coderai-missing-${process.pid}`, 'coderai');
  await assert.rejects(exec(process.execPath, ['test', resolve('test/native.test.mjs')], {
    env: { ...process.env, CODERAI_TEST_BINARY: missing }, timeout: 15000,
  }), error => {
    assert.match(error.stderr, /Standalone executable not found/);
    return true;
  });
});
