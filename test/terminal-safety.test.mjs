import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
import { fixture, cli } from './fixture.mjs';

test('real PTY: fragmented model escape sequences never reach the terminal', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = ''; let err = '';
  child.stdout.on('data', chunk => out += chunk); child.stderr.on('data', chunk => err += chunk);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: process.execPath, cli, env: { ...f.env, TERM: 'xterm-256color' }, actions: [
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
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: process.execPath, cli, env: { ...f.env, TERM: 'xterm-256color' }, args: ['--trust', '-p', 'SCENARIO terminal-escape'], actions: [
    { wait: 'Safe  text' },
  ] }));
  const code = await new Promise((resolve, reject) => { child.on('error', reject); child.on('close', resolve); });
  assert.equal(code, 0, err);
  const result = JSON.parse(out);
  assert.equal(result.code, 0);
  assert.doesNotMatch(Buffer.from(result.raw, 'base64').toString('utf8'), /TERMINAL_INJECTION/);
});
