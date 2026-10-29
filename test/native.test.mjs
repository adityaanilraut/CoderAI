import { test } from 'node:test';
import assert from 'node:assert/strict';
import { copyFile, chmod, mkdtemp, readFile, rm, mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fixture } from './fixture.mjs';

const builtBinary = resolve('dist-native', `${process.platform}-${process.arch}`, process.platform === 'win32' ? 'coderai.exe' : 'coderai');
test('standalone embeds and loads its native clipboard binding', { timeout: 30000, skip: process.platform !== 'darwin' || !existsSync(builtBinary) }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const dir = join(f.root, 'tmp'); const bin = join(f.root, 'bin'); await mkdir(dir); await mkdir(bin);
  const file = join(f.cwd, 'pixel.png');
  await writeFile(file, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64'));
  const marker = join(f.root, 'binding-loaded');
  await writeFile(join(bin, 'osascript'), '#!/bin/sh\nls "$TMPDIR"/coderai-native-clipboard-*/clipboard.node > "$CODERAI_BINDING_MARKER"\nprintf "%s" "$CODERAI_CLIPBOARD_RESULT"\n', { mode: 0o755 });
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
test('standalone executable runs the engine and tools outside the checkout', { timeout: 60000, skip: !existsSync(builtBinary) && 'run build:native first' }, async t => {
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
