import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fixture, cli, testRuntime } from './fixture.mjs';
const { StdinBuffer } = await import('../packages/pi-tui/src/stdin-buffer.ts');
const { setTimeout: delay } = await import('node:timers/promises');

test('OSC replies can reassemble across slow chunks while ordinary Escape and default buffering stay unchanged', async () => {
  const input = new StdinBuffer({ timeout: 5, escapeTimeout: 5, oscTimeout: 200 });
  const events = []; input.on('data', value => events.push(value));
  input.process('\x1b]11;rgb:ffff'); await delay(40); assert.deepEqual(events, []);
  input.process('/ffff/ffff\x07'); assert.deepEqual(events, ['\x1b]11;rgb:ffff/ffff/ffff\x07']);
  input.process('\x1b'); await delay(15); assert.equal(events.at(-1), '\x1b'); input.destroy();
  const ordinary = new StdinBuffer({ timeout: 5 }); const defaults = []; ordinary.on('data', value => defaults.push(value));
  ordinary.process('\x1b]11;rgb:ffff'); await delay(15); assert.equal(defaults.length, 1); ordinary.destroy();
});

test('real PTY: fragmented model escape sequences never reach the terminal', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = ''; let err = '';
  child.stdout.on('data', chunk => out += chunk); child.stderr.on('data', chunk => err += chunk);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: testRuntime, cli, env: { ...f.env, TERM: 'xterm-256color' }, actions: [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO terminal-escape\r' },
    { wait: 'Safe  text', send: '/quit\r' },
  ] }));
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  assert.equal(code, 0, err);
  const result = JSON.parse(out);
  assert.equal(result.code, 0);
  assert.ok(result.terminalRestored);
  assert.doesNotMatch(Buffer.from(result.raw, 'base64').toString('utf8'), /TERMINAL_INJECTION/);
});

test('real PTY: headless text also filters fragmented model escape sequences', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = ''; let err = '';
  child.stdout.on('data', chunk => out += chunk); child.stderr.on('data', chunk => err += chunk);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: testRuntime, cli, env: { ...f.env, TERM: 'xterm-256color' }, args: ['--trust', '-p', 'SCENARIO terminal-escape'], actions: [
    { wait: 'Safe  text' },
  ] }));
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  assert.equal(code, 0, err);
  const result = JSON.parse(out);
  assert.equal(result.code, 0);
  assert.doesNotMatch(Buffer.from(result.raw, 'base64').toString('utf8'), /TERMINAL_INJECTION/);
});

test('real PTY: skills list displays plain text for styled project metadata', { timeout: 30000 }, async t => {
  const f = await fixture();
  t.after(f.cleanup);
  const skill = join(f.cwd, '.coderai', 'skills', 'formatting-fixture');
  await mkdir(skill, { recursive: true });
  const description = JSON.stringify('Sample \x1b[32mstyled\x1b[0m text');
  await writeFile(join(skill, 'SKILL.md'), `---\nname: formatting-fixture\ndescription: ${description}\n---\nFixture skill.\n`);

  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  child.stderr.on('data', chunk => { stderr += chunk; });
  const closed = new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  child.stdin.end(JSON.stringify({
    cwd: f.cwd, node: testRuntime, cli, env: { ...f.env, TERM: 'xterm-256color' },
    args: ['skills', 'list'], actions: [{ wait: 'Sample styled text' }],
  }));
  assert.equal(await closed, 0, stderr);
  const result = JSON.parse(stdout);
  assert.equal(result.code, 0, result.output);
  assert.doesNotMatch(Buffer.from(result.raw, 'base64').toString('utf8'), /\x1b\[32mstyled/);
});

test('fullscreen search strips terminal controls from bracketed paste before rendering or querying', async () => {
  const { AltScreenSearchComponent } = await import('../packages/pi-tui/src/alt-screen-search.ts');
  const queries = [];
  const search = new AltScreenSearchComponent(value => queries.push(value));
  search.handleInput('\x1b[200~find\x1b]52;c;INJECTED\x07\x1b[31m safe\x1b[201~');
  assert.equal(queries.at(-1), 'find safe');
  assert.doesNotMatch(search.render(60).join('\n'), /INJECTED|\x1b\]52|\x1b\[31m/);
});
