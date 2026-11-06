import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { readFile, writeFile, access, readdir, mkdir, realpath } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fixture, cli, testRuntime } from './fixture.mjs';

const terminal = (f, actions, args = [], size = {}) => new Promise((done, reject) => {
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = ''; let err = ''; child.stdout.on('data', c => out += c); child.stderr.on('data', c => err += c);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, ...(process.env.CODERAI_TEST_BINARY ? { executable: resolve(process.env.CODERAI_TEST_BINARY) } : { node: testRuntime, cli }), env: { ...f.env, TERM: 'xterm-256color' }, actions, args, ...size }));
  child.on('error', reject); child.on('close', code => code === 0 ? done(JSON.parse(out)) : reject(new Error(err + out)));
});

test('real PTY: live todo cards update, expand through /todos, complete and clear', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO ui-todos\r' },
    { wait: 'Todo fixture ready.', send: '/todos\r' },
    { wait: '○ Last pending item', send: 'SCENARIO ui-todo-complete\r' },
    { wait: 'Todo fixture completed.', send: 'SCENARIO ui-todo-clear\r' },
    { wait: 'Todo fixture cleared.', send: '/todos\r' },
    { wait: 'No todos.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /Todos · 2\/5 done · 1 active/);
  assert.match(result.output, /● Active task/);
  assert.match(result.output, /✓ Latest finished/);
  assert.match(result.output, /Todos · 5\/5 done/);
  assert.doesNotMatch(result.output, /\[in_progress\]|__kimi_env_model__|queued 0/);
});

test('real PTY: resumed todos use cards and a new session clears the pinned list', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const seed = await f.run(['--trust', '-p', 'SCENARIO ui-todos']);
  assert.equal(seed.code, 0, seed.stderr);
  const result = await terminal(f, [
    { wait: 'Idle', send: '/todos\r' },
    { wait: '○ Last pending item', send: '/new\r' },
    { wait: 'New session', send: '/todos\r' },
    { wait: 'No todos.', send: '/quit\r' },
  ], ['--continue']);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /Todos · 2\/5 done · 1 active/);
  assert.doesNotMatch(result.output, /\[in_progress\]/);
});

test('real PTY: provider errors show recovery and the UI remains usable', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO auth\r' },
    { wait: 'coderai --setup', send: '/new\r' },
    { wait: 'New session', send: 'hello after recovery\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /Error details · Ctrl\+O to show details/);
  assert.doesNotMatch(result.output, /"retryable"|"agentId"/);
});

test('real PTY: retry shows attempts and provider reason while status stays available', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO ui-retry\r' },
    { wait: 'Retrying (', send: '/status\r' },
    { wait: 'Thinking effort:' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /Retrying \(2\/\d+\) · in \d+s/);
  assert.match(result.output, /429 · Fixture rate limit/);
  assert.equal(f.requests.length, 2);
});

test('real PTY: compact footer and cancellation work in a 50 by 16 terminal', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'Idle', send: '/plan on\r' },
    { wait: 'Plan mode: on', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '\u0003' },
    { wait: 'Turn cancelled', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /\[PLAN\]/);
  assert.match(result.output, /Context 0%/);
});
test('real PTY: startup trust, chat, approval, denial, slash commands and terminal restoration', { timeout: 90000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.cwd, 'startup-shell'), `#!/bin/sh\ntouch '${join(f.cwd, 'startup-ran')}'\n`, { mode: 0o755 });
  f.env.SHELL = join(f.cwd, 'startup-shell');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'hello\r' },
    { wait: 'Hello from the deterministic provider.', send: '/help\r' },
    { wait: 'CoderAI help', send: '\u001b' },
    { wait: 'Idle', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: 'no\r' },
    { wait: 'The requested action was denied.', send: '/new\r' },
    { wait: 'New session', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: 'yes\r' },
    { wait: 'Tool completed:', send: '/context\r' },
    { wait: 'messages', send: '/compact\r' },
    { wait: 'Compaction requested', send: '/clear\r' },
    { wait: 'Context cleared', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0); assert.ok(result.terminalRestored); assert.match(result.output, /CoderAI/);
  await assert.rejects(access(join(f.cwd, 'startup-ran')));
  assert.equal(await readFile(join(f.cwd, 'denied.txt'), 'utf8'), 'should not exist');
  assert.equal((await readdir(join(f.home, 'locks'), { recursive: true })).filter(p => p.endsWith('.lock')).length, 0);
});
test('real PTY: Ctrl+C interrupts work, then Ctrl+C exits idle terminal', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '\u0003' }, { wait: 'Turn cancelled', send: '\u0003' },
  ]);
  assert.equal(result.code, 0); assert.ok(result.terminalRestored);
});

test('real PTY: Ctrl+C clears an unsent draft before exiting', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'unfinished draft' },
    { wait: 'unfinished draft', send: '\u0003' },
    { wait: 'Draft cleared.', send: '\u0003' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: title, status and latest resume work in the current workspace', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/title Review session\r' },
    { wait: 'Title: Review session', send: '/status\r' },
    { wait: 'Plan mode:', send: '/resume latest\r' },
    { wait: 'Resumed ', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: queued prompts run after the active turn and session picker resumes', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO deny-draft\r' },
    { wait: 'Working', send: 'hello queued\r' },
    { wait: 'Queued (1): hello queued', send: '' },
    { wait: 'Approve Write?', send: 'no\r' },
    { wait: 'You: hello queued', send: '' },
    { wait: 'Idle', send: '/resume\r' },
    { wait: 'Choose a conversation:', send: '\r' },
    { wait: 'Resumed ', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: queued input can be removed while work is running', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: 'do not run this\r' },
    { wait: 'Queued (1):', send: '/queue clear\r' },
    { wait: 'Queue cleared.', send: '\u0003' },
    { wait: 'Turn cancelled', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.doesNotMatch(result.output, /You: do not run this/);
  assert.match(result.output, /Queued input · \/queue to manage/);
  assert.ok(result.terminalRestored);
});

test('real PTY: queued input can be recalled and edited while work is running', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: 'queued draft\r' },
    { wait: 'Queued (1): queued draft', send: '\u001b[A' },
    { wait: 'Queued input recalled.', send: '\u0003' },
    { wait: 'Turn cancelled', send: '\u0003' },
    { wait: 'Draft cleared.', send: '/sessions\r' },
    { wait: 'Choose a conversation:', send: '\u001b' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.doesNotMatch(result.output, /You: queued draft/);
  assert.match(result.output, /CoderAI · Ready to code/);
  assert.ok(result.terminalRestored);
});

test('real PTY: Ctrl+S steers an active turn', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: 'new direction\u0013' },
    { wait: 'Steering sent', send: '\u0003' },
    { wait: 'Turn cancelled', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: double Escape opens undo and command aliases work', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'hello\r' },
    { wait: 'Hello from the deterministic provider.', send: '\u001b' },
    { wait: 'Idle', send: '\u001b' },
    { wait: 'Withdraw prompts from context', send: '\u001b' },
    { wait: 'Idle', send: '/h\r' },
    { wait: 'CoderAI help', send: '\u001b' },
    { wait: 'Idle', send: '/q\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: status is available during a running turn', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '/status\r' },
    { wait: 'Workspace:', send: '\u0003' },
    { wait: 'Turn cancelled', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.doesNotMatch(result.output, /Queued \(1\): \/status/);
});

test('real PTY: queued prompts retain the attachments staged at submission', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  await writeFile(join(f.cwd, 'pixel.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64'));
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '/attach pixel.png\r' },
    { wait: 'Attachment staged:', send: 'recovered describe queued image\r' },
    { wait: 'Queued (1):', send: '/attach clear\r' },
    { wait: 'Attachments cleared.', send: '\u0003' },
    { wait: 'You: recovered describe queued image', send: '' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('data:image/png;base64,')));
});

test('real PTY: cancelled resume preserves attachments and session switch clears them', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await f.run(['--trust', '-p', 'ATTACHMENT_TARGET']);
  const result = await terminal(f, [
    { wait: 'Idle', send: '/attach pixel.png\r' },
    { wait: 'Attachment staged:', send: '/resume\r' },
    { wait: 'Choose a conversation:', send: '\u001b' },
    { wait: 'Idle', send: '/attach list\r' },
    { wait: 'Attachments for the next prompt:', send: '/sessions\r' },
    { wait: 'Choose a conversation:', send: 'ATTACHMENT_TARGET\r' },
    { wait: 'Resumed ', send: '/attach list\r' },
    { wait: 'No attachments staged.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: exports the persisted conversation without overwriting an existing file', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'hello export\r' },
    { wait: 'Hello from the deterministic provider.', send: '/export-md transcript.md\r' },
    { wait: 'Exported conversation', send: '/export-md transcript.md\r' },
    { wait: 'EEXIST', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.match(await readFile(join(f.cwd, 'transcript.md'), 'utf8'), /hello export/);
  assert.ok(result.terminalRestored);
});

test('real PTY: export includes tool activity and turns removed from model context', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const prior = await f.run(['--trust', '--yolo', '-p', 'SCENARIO deny']);
  assert.equal(prior.code, 0, prior.stderr);
  const result = await terminal(f, [
    { wait: 'Tool completed: Write', send: '/clear\r' },
    { wait: 'Context cleared', send: '/export-md history.md\r' },
    { wait: 'Exported conversation', send: '/quit\r' },
  ], ['--continue']);
  assert.equal(result.code, 0, result.output);
  const markdown = await readFile(join(f.cwd, 'history.md'), 'utf8');
  assert.match(markdown, /SCENARIO deny/);
  assert.match(markdown, /Tool call: Write/);
  assert.match(markdown, /Tool result/);
  assert.ok(result.terminalRestored);
});

test('real PTY: executes a user shell command without sending it as a model prompt', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '!printf shell-result\r' },
    { wait: 'Shell command completed.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /shell-result/);
  assert.equal(f.requests.length, 0);
  assert.ok(result.terminalRestored);
});

test('real PTY: completes workspace file mentions', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.cwd, 'visible-file.txt'), 'example');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '@visible' },
    { wait: 'visible-file.txt', send: '\t\r' },
    { wait: 'You: @visible-file.txt', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: resumed transcript includes recorded tool calls and results', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const prior = await f.run(['--trust', '--yolo', '-p', 'SCENARIO deny']);
  assert.equal(prior.code, 0, prior.stderr);
  const result = await terminal(f, [
    { wait: 'Tool completed: Write', send: '\u000f' },
    { wait: 'Output', send: '/quit\r' },
  ], ['--continue']);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: browses and stops a background task', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO background\r' },
    { wait: 'Started background task.', send: '/tasks\r' },
    { wait: 'Choose a background task:', send: '\r' },
    { wait: 'Task action:', send: '\u001b[B\r' },
    { wait: 'Stop requested', send: '/quit\r' },
  ], ['--yolo']);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: attachment labels and removal preserve the draft at 50 by 16', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.cwd, 'pixel.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64'));
  execFileSync('git', ['-C', f.cwd, 'init', '-b', 'phase-three'], { stdio: 'pipe' });
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'git:phase-three dirty', send: '/attach pixel.png\r' },
    { wait: 'image/png', send: 'draft after removal\u0014' },
    { wait: 'Remove staged attachment', send: '\r' },
    { wait: 'Idle', send: '\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(result.output, /Image preview unavailable/);
  assert.match(JSON.stringify(f.requests.at(-1)), /draft after removal/);
  assert.doesNotMatch(JSON.stringify(f.requests.at(-1)), /data:image/);
});

test('real PTY: task output pages, pauses, follows and returns to its draft at 50 by 16', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO ui-task-output\r' },
    { wait: 'Started output fixture.', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: 'draft survives recovered\u0002' },
    { wait: 'Choose a background task:', send: '\r' },
    { wait: 'Task action:', send: '\r' },
    { wait: 'Task output' },
    { wait: 'LOG-060', send: '\u001b[5~' },
    { wait: 'Paused' },
    { wait: 'new output', send: '\u001b[F' },
    { wait: 'LIVE-TAIL', send: '\u001b[H' },
    { wait: 'LOG-001', send: '\u001b[6~' },
    { wait: 'LOG-009', send: '\u001b' },
    { wait: 'Working', send: '\u001b' },
    { wait: 'Turn cancelled', send: '\r' },
    { wait: 'Started output fixture.', send: '/quit\r' },
  ], ['--yolo'], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(result.output, /Following/);
  assert.match(JSON.stringify(f.requests.at(-1)), /draft survives/);
});

test('real PTY: invokes a discovered project skill', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const skillDir = join(f.cwd, '.coderai', 'skills', 'greet');
  await mkdir(skillDir, { recursive: true });
  await writeFile(join(skillDir, 'SKILL.md'), '---\nname: greet\ndescription: A test greeting skill\n---\n\nSKILL_GREETING_MARKER\n');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/skill:greet say hello\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('SKILL_GREETING_MARKER')));
  assert.ok(result.terminalRestored);
});

test('real PTY: local plugin lifecycle exposes its qualified skill', { timeout: 60000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  await writeFile(join(f.cwd, 'pixel.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6UAAAAABJRU5ErkJggg==', 'base64'));
  const plugin = join(f.root, 'example-plugin');
  const skillDir = join(plugin, 'skills', 'greeting');
  await mkdir(skillDir, { recursive: true });
  await writeFile(join(plugin, 'kimi.plugin.json'), JSON.stringify({ name: 'fixture-plugin', version: '1.0.0', skills: './skills/' }));
  await writeFile(join(skillDir, 'SKILL.md'), '---\nname: greeting\ndescription: Fixture plugin greeting\n---\n\nPLUGIN_SKILL_MARKER\n');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: `/plugins install ${plugin}\r` },
    { wait: 'Install plugin?', send: '\x1b[B\r' },
    { wait: 'Plugin installed:', send: '/plugins list\r' },
    { wait: 'fixture-plugin', send: '/attach pixel.png\r' },
    { wait: 'Attachment staged:', send: '/skill:fixture-plugin:greeting hello\r' },
    { wait: 'Hello from the deterministic provider.', send: '/plugins disable fixture-plugin\r' },
    { wait: 'Plugin fixture-plugin disabled', send: '/plugins enable fixture-plugin\r' },
    { wait: 'Plugin fixture-plugin enabled', send: '/plugins remove fixture-plugin\r' },
    { wait: 'Plugin fixture-plugin removed', send: '/plugins list\r' },
    { wait: 'No plugins installed.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('PLUGIN_SKILL_MARKER')));
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('data:image/png;base64,')));
  assert.ok(result.terminalRestored);
});

test('real PTY: question choices return the selected label', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO question\r' },
    { wait: 'Choose a color?', send: '\u001b[B\r' },
    { wait: 'Question answered.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('Blue')));
  assert.ok(result.terminalRestored);
});

test('real PTY: multi-select keeps choices visible and submits both answers', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO multi-question\r' },
    { wait: 'Choose colors?', send: '\r' },
    { wait: '[x] Red', send: '\u001b[B\r' },
    { wait: '[x] Blue', send: '\u001b[B\u001b[B\r' },
    { wait: 'Question answered.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('Red, Blue')));
  assert.ok(result.terminalRestored);
});

test('real PTY: copies the latest assistant text using OSC 52', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'hello\r' },
    { wait: 'Hello from the deterministic provider.', send: '/copy\r' },
    { wait: 'Clipboard:', send: '/quit\r' },
  ]);
  const encoded = Buffer.from('Hello from the deterministic provider.').toString('base64');
  assert.ok(Buffer.from(result.raw, 'base64').includes(Buffer.from(`\x1b]52;c;${encoded}\x07`)));
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: saves the selected theme', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/theme light\r' },
    { wait: 'Theme: light', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.equal(await readFile(join(f.home, 'theme'), 'utf8'), 'light\n');
  assert.ok(result.terminalRestored);
});

test('real PTY: external editor restores the terminal and inserts its draft', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const editor = join(f.root, 'edit-prompt');
  await writeFile(editor, '#!/bin/sh\nprintf "edited prompt" > "$1"\n', { mode: 0o755 });
  f.env.EDITOR = editor;
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '\u0007' },
    { wait: 'edited prompt', send: '\r' },
    { wait: 'You: edited prompt', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: toggles and inspects engine modes', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/swarm on\r' },
    { wait: 'Start Swarm with approvals on?', send: '\x1b[B\x1b[B\r' },
    { wait: 'Swarm mode: on', send: '/swarm status\r' },
    { wait: 'Swarm mode: on', send: '/swarm off\r' },
    { wait: 'Swarm mode: off', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: follows an autonomous goal through a continuation', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/goal SCENARIO goal\r' },
    { wait: 'Goal progress.', send: '' },
    { wait: 'Goal finished.', send: '/goal status\r' },
    { wait: 'No active goal.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: pauses an active autonomous goal', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/goal SCENARIO wait\r' },
    { wait: 'Working', send: '/goal pause\r' },
    { wait: 'Goal paused.', send: '/goal status\r' },
    { wait: 'Status: paused', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: queues upcoming goals, preserves them across resume, and permits removal', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const first = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/goal SCENARIO wait\r' },
    { wait: 'Working', send: '/goal next write release notes\r' },
    { wait: 'Upcoming goal queued:', send: '/goal pause\r' },
    { wait: 'Goal paused.', send: '/goal next list\r' },
    { wait: '1. write release notes', send: '/quit\r' },
  ]);
  assert.equal(first.code, 0, first.output);
  const resumed = await terminal(f, [
    { wait: 'Idle', send: '/goal next list\r' },
    { wait: '1. write release notes', send: '/goal next remove 1\r' },
    { wait: 'Upcoming goals updated.', send: '/goal next list\r' },
    { wait: 'No upcoming goals.', send: '/quit\r' },
  ], ['--continue']);
  assert.equal(resumed.code, 0, resumed.output);
  assert.ok(resumed.terminalRestored);
});

test('real PTY: promotes a queued goal after the current goal completes', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/goal SCENARIO goal-pause\r' },
    { wait: 'Working', send: '/goal next follow-up objective\r' },
    { wait: 'Upcoming goal queued:', send: '/goal pause\r' },
    { wait: 'Goal paused.', send: '/goal resume\r' },
    { wait: 'Starting upcoming goal: follow-up objective', send: '/goal pause\r' },
    { wait: 'Goal paused.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: plan controls and readable empty lists', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/plan on\r' },
    { wait: 'Plan mode: on', send: '/plan off\r' },
    { wait: 'Plan mode: off', send: '/todos\r' },
    { wait: 'No todos.', send: '/add-dir list\r' },
    { wait: 'No additional directories.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: in-session side question preserves main context without creating another session', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'main context marker\r' },
    { wait: 'Hello from the deterministic provider.', send: '/btw side-only marker\r' },
    { wait: 'Q: side-only marker' },
    { wait: 'Completed', send: '/btw close\r' },
    { wait: 'Side question closed.', send: 'main follow-up\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('side-only marker')));
  const finalRequest = JSON.stringify(f.requests.at(-1));
  assert.match(finalRequest, /main context marker/);
  assert.match(finalRequest, /main follow-up/);
  assert.doesNotMatch(finalRequest, /side-only marker/);
  const sessions = await f.run(['sessions']);
  assert.equal(sessions.code, 0, sessions.stderr);
  assert.equal(sessions.stdout.trim().split('\n').length, 1);
  assert.ok(result.terminalRestored);
});

test('real PTY: /btw answers and accepts follow-ups while the main turn stays busy', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '/btw side during busy\r' },
    { wait: 'Q: side during busy' },
    { wait: 'Completed', send: 'side follow-up\r' },
    { wait: 'Q: side follow-up' },
    { wait: 'Completed', send: '/btw close\r' },
    { wait: 'Working', send: '\u0003' },
    { wait: 'Turn cancelled', send: '/auto-title off\r' },
    { wait: 'Automatic session titles: off', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.doesNotMatch(result.output, /Queued \(.*\): \/btw/);
  const sideRequests = f.requests.filter(request => JSON.stringify(request).includes('side-channel conversation'));
  assert.equal(sideRequests.length, 2); assert.match(JSON.stringify(sideRequests[1]), /side during busy.*side follow-up/s);
  assert.match(await readFile(join(f.home, 'tui.toml'), 'utf8'), /auto_session_title = false/);
  const sessions = await f.run(['sessions', '--json']); assert.equal(JSON.parse(sessions.stdout).length, 1);
});

test('real PTY: stages an image for one prompt and reports invalid attachments', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  await writeFile(join(f.cwd, 'pixel image.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64'));
  await writeFile(join(f.cwd, 'invalid.png'), 'not an image');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/attach pixel image.png\r' },
    { wait: 'Attachment staged:', send: 'Describe this image\r' },
    { wait: 'Hello from the deterministic provider.', send: '/attach list\r' },
    { wait: 'No attachments staged.', send: '/attach invalid.png\r' },
    { wait: 'Attachment staged: invalid.png', send: 'Describe this file\r' },
    { wait: 'Unsupported attachment format', send: '/attach clear\r' },
    { wait: 'Attachments cleared.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.match(JSON.stringify(f.requests[0]), /data:image\/png;base64,/);
  assert.equal(f.requests.length, 1);
  assert.ok(result.terminalRestored);
});

test('real PTY: clipboard paste stages an image before immediately submitted text', { timeout: 30000, skip: process.platform !== 'darwin' }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  const bin = join(f.root, 'clipboard-bin'); await mkdir(bin);
  await writeFile(join(bin, 'osascript'), '#!/bin/sh\nsleep 1\nprintf "%s" "$CODERAI_TEST_CLIPBOARD"\n', { mode: 0o755 });
  f.env.PATH = `${bin}:${process.env.PATH}`;
  f.env.CODERAI_TEST_CLIPBOARD = JSON.stringify({ image: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=' });
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '\u0016Describe clipboard\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.match(JSON.stringify(f.requests[0]), /data:image\/png;base64,/);
  assert.match(JSON.stringify(f.requests[0]), /Describe clipboard/);
  assert.ok(result.terminalRestored);
});

test('real PTY: resumes a paused goal and follows it to completion', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/goal SCENARIO goal-pause\r' },
    { wait: 'Working', send: '/goal pause\r' },
    { wait: 'Goal paused.', send: '/goal resume\r' },
    { wait: 'Resumed goal finished.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: undo withdraws context and restores the prompt as a draft', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'first prompt\r' },
    { wait: 'Hello from the deterministic provider.', send: 'second prompt\r' },
    { wait: 'Hello from the deterministic provider.', send: '/undo 1\r' },
    { wait: 'Withdraw prompts only', send: '\r' },
    { wait: 'file changes remain.', send: '\u0003' },
    { wait: 'Draft cleared.', send: '\u0003' },
  ]);
  assert.equal(result.code, 0, result.output);
  const continuation = await f.run(['--continue', '-p', 'after undo']);
  assert.equal(continuation.code, 0, continuation.stderr);
  const messages = JSON.stringify(f.requests.at(-1).messages);
  assert.match(messages, /first prompt/);
  assert.doesNotMatch(messages, /second prompt/);
});

test('real PTY: Ctrl+D and SIGTERM close a pending startup trust dialog', { timeout: 40000 }, async t => {
  for (const action of [{ send: '\u0004' }, { signal: 'SIGTERM' }]) {
    const f = await fixture();
    try {
      const result = await terminal(f, [{ wait: 'Trust ', ...action }]);
      assert.equal(result.code, 0, result.output);
      assert.ok(result.terminalRestored);
      assert.doesNotMatch(result.output, /Shutdown timed out/);
      assert.equal(f.requests.length, 0);
    } finally { await f.cleanup(); }
  }
});
test('real PTY: an existing yes draft cannot approve a newly opened dialog', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO deny-draft\r' },
    { wait: 'Working', send: 'yes' },
    { wait: 'Approve Write?', send: '\r' },
    { wait: 'The requested action was denied.', send: '\u0003' },
    { wait: 'Draft cleared.', send: '\u0003' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  await assert.rejects(access(join(f.cwd, 'denied.txt')));
});
test('real PTY: Esc discards a partial approval answer', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: 'yes\u001b' },
    { wait: 'The requested action was denied.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  await assert.rejects(access(join(f.cwd, 'denied.txt')));
});

test('real PTY: rejection feedback reaches the model', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: 'no: use another file\r' },
    { wait: 'The requested action was denied.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('use another file')));
  await assert.rejects(access(join(f.cwd, 'denied.txt')));
});
test('real PTY: /new checks trust for the invocation directory after resuming another workspace', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const initial = await f.run(['--trust', '--json', '-p', 'hello']);
  assert.equal(initial.code, 0, initial.stderr);
  const id = JSON.parse(initial.stdout.split('\n')[0]).sessionId;
  const other = join(f.root, 'untrusted');
  await mkdir(other);
  await writeFile(join(other, 'AGENTS.md'), 'UNTRUSTED_INSTRUCTION_MARKER\n');
  const result = await terminal({ ...f, cwd: other }, [
    { wait: 'Idle', send: '/new\r' },
    { wait: 'WORKSPACE TRUST', send: 'no\r' },
    { wait: 'Workspace is not trusted', send: 'hello after denial\r' },
    { wait: 'Hello from the deterministic provider.', send: '/new\r' },
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'New session', send: 'hello after trust\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ], ['--resume', id]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  const deniedRequest = f.requests.find(r => JSON.stringify(r.messages).includes('hello after denial'));
  const trustedRequest = f.requests.find(r => JSON.stringify(r.messages).includes('hello after trust'));
  assert.ok(deniedRequest);
  assert.doesNotMatch(JSON.stringify(deniedRequest), /UNTRUSTED_INSTRUCTION_MARKER/);
  assert.match(JSON.stringify(trustedRequest), /UNTRUSTED_INSTRUCTION_MARKER/);
});

test('real PTY: approval window enables YOLO and /yolo can inspect and disable it', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: '3\r' },
    { wait: 'Tool completed:', send: '/yolo status\r' },
    { wait: 'YOLO: on', send: '/yolo off\r' },
    { wait: 'YOLO disabled', send: '/yolo\r' },
    { wait: 'YOLO enabled', send: '/yolo\r' },
    { wait: 'YOLO disabled', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.equal(await readFile(join(f.cwd, 'denied.txt'), 'utf8'), 'should not exist');
});

test('real PTY: approval selector rejects with feedback and restores manual mode after --yolo', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/yolo status\r' },
    { wait: 'YOLO: on', send: '/yolo off\r' },
    { wait: 'YOLO disabled', send: 'SCENARIO deny\r' },
    { wait: 'Approve Write?', send: '4\r' },
    { wait: 'Why should this action change?', send: 'Use another file\r' },
    { wait: 'The requested action was denied.', send: '/quit\r' },
  ], ['--yolo']);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('Use another file')));
  await assert.rejects(access(join(f.cwd, 'denied.txt')));
});

test('real PTY: workspace trust selector saves trust and uses trust-specific instructions', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: '\u001b[A\r' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
  assert.match(result.output, /Waiting for workspace trust/);
  assert.doesNotMatch(result.output, /Waiting for approval|type yes to approve|Type yes to trust, or Enter to exit/);
  const saved = await f.run(['-p', 'hello']);
  assert.equal(saved.code, 0, saved.stderr);
});

test('real PTY: Enter and Esc leave an untrusted workspace without making requests', { timeout: 30000 }, async t => {
  for (const send of ['\r', 'yes\u001b']) {
    const f = await fixture();
    try {
      const result = await terminal(f, [{ wait: 'WORKSPACE TRUST', send }]);
      assert.equal(result.code, 1, result.output);
      assert.ok(result.terminalRestored);
      assert.equal(f.requests.length, 0);
      const untrusted = await f.run(['-p', 'hello']);
      assert.equal(untrusted.code, 1, untrusted.stderr);
      assert.match(untrusted.stderr, /Workspace is not trusted/);
    } finally { await f.cleanup(); }
  }
});

test('real PTY: workspace trust accepts a numbered choice for a directory containing spaces', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const cwd = join(f.root, 'untitled folder 2');
  await mkdir(cwd);
  const result = await terminal({ ...f, cwd }, [
    { wait: 'WORKSPACE TRUST', send: '1\r' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output);
  assert.ok(result.terminalRestored);
});

test('real PTY: thinking and grouped reads expand, collapse, and survive resume', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const first = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO ui-transcript\r' },
    { wait: 'Transcript fixture complete.', send: '\u000f' },
    { wait: 'Reviewing the fixture files before answering.' },
    { wait: 'TEST_INSTRUCTION_42', send: '\u000f' },
    { wait: 'Read 2 files · completed', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(first.code, 0, first.output);
  assert.ok(first.terminalRestored);
  assert.match(first.output, /Thinking · Ctrl\+O expand/);
  const resumed = await terminal(f, [
    { wait: 'Transcript fixture complete.', send: '\u000f' },
    { wait: 'Reviewing the fixture files before answering.' },
    { wait: 'TEST_INSTRUCTION_42', send: '/quit\r' },
  ], ['--continue'], { columns: 50, rows: 16 });
  assert.equal(resumed.code, 0, resumed.output);
  assert.ok(resumed.terminalRestored);
  assert.match(resumed.output, /Read 2 files · completed/);
});

test('real PTY: help pages close cleanly and model search returns to the conversation', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/help\r' },
    { wait: 'KEYBOARD SHORTCUTS', send: '\u001b[F' },
    { wait: '/cron', send: '\u001b' },
    { wait: 'Idle', send: '/model\r' },
    { wait: 'Choose a provider:', send: 'OpenAI\r' },
    { wait: 'Choose a model from OpenAI:', send: 'fixture\r' },
    { wait: 'Model:', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(result.output, /\[current\] fixture/);
});

test('real PTY: prompt history survives restart and stays scoped to its workspace', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const first = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'remember this prompt 73\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(first.code, 0, first.output);
  const second = await terminal(f, [
    { wait: 'Idle', send: '\u001b[A' },
    { wait: 'remember this prompt 73', send: '\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(second.code, 0, second.output); assert.ok(second.terminalRestored);
  const other = join(f.root, 'other-history-project'); await mkdir(other);
  const third = await terminal({ ...f, cwd: other }, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '\u001b[A/quit\r' },
  ]);
  assert.equal(third.code, 0, third.output); assert.ok(third.terminalRestored);
  assert.equal(f.requests.length, 2);
});

test('real PTY: session browser filters workspaces, renames and deletes a saved conversation', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const saved = await f.run(['--trust', '-p', 'old saved conversation']); assert.equal(saved.code, 0, saved.stderr);
  const result = await terminal(f, [
    { wait: 'Idle', send: '/sessions\r' },
    { wait: 'Choose a conversation:', send: '\u0001' },
    { wait: 'All workspaces', send: 'old saved\u0012' },
    { wait: 'Rename conversation', send: '\u0001\u000bRenamed conversation\r' },
    { wait: 'Title: Renamed conversation', send: 'Renamed\u0018' },
    { wait: 'Delete conversation?', send: '\u001b[B\r' },
    { wait: 'Conversation deleted.', send: '\u001b' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  const listing = await f.run(['sessions']); assert.doesNotMatch(listing.stdout, /Renamed conversation/);
});

test('real PTY: guided setup launched from the model picker returns safely', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/model\r' },
    { wait: 'Choose a provider:', send: '\u001b[F\r' },
    { wait: 'Finish setup', send: '\u001b' },
    { wait: 'Choose a provider:', send: '\u001b' },
    { wait: 'Idle', send: '/setup\r' },
    { wait: 'Finish setup', send: '\u0003' },
    { wait: 'Setup closed. Configuration refreshed.', send: '\u001b[200~after setup\u001b[201~\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
});

test('real PTY: auto theme consumes split and late background replies without changing the draft', { timeout: 35000 }, async t => {
  for (const delayMs of [0, 400]) {
    const f = await fixture(); t.after(f.cleanup);
    delete f.env.NO_COLOR; f.env.FORCE_COLOR = '3';
    const result = await terminal(f, [
      { wait: 'WORKSPACE TRUST', send: 'yes\r' },
      { wait: 'Idle', send: '/help\r' },
      { wait: 'KEYBOARD SHORTCUTS', send: '\u001b' },
      { wait: 'Idle', send: 'draft after terminal detection\r' },
      { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
    ], [], { columns: 50, rows: 16, backgroundReply: 'rgb:ffff/ffff/ffff', backgroundDelayMs: delayMs, splitBackgroundReply: true });
    assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
    assert.equal(result.backgroundQueries, 1);
    const raw = Buffer.from(result.raw, 'base64').toString();
    assert.match(raw, delayMs ? /\x1b\[(?:38;2;97;175;239|38;5;111)m/ : /\x1b\[(?:38;2;0;90;156|38;5;31)m/);
    assert.doesNotMatch(JSON.stringify(f.requests), /rgb:|ffff/);
    assert.match(JSON.stringify(f.requests), /draft after terminal detection/);
  }
});

test('real PTY: welcome, dialogs and setup resize in a long-path light-theme workspace', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const cwd = join(f.cwd, ...Array.from({ length: 5 }, () => 'a long directory name'), 'project 日'); await mkdir(cwd, { recursive: true });
  delete f.env.NO_COLOR; f.env.FORCE_COLOR = '3';
  await writeFile(join(f.home, 'theme'), 'light\n');
  const result = await terminal({ ...f, cwd }, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'Idle', send: 'draft survives resize' , resize: { columns: 80, rows: 24 } },
    { wait: 'draft survives resize', resize: { columns: 50, rows: 16 }, send: '\u0002' },
    { wait: 'No background tasks.', send: '\u0015/setup\r' },
    { wait: 'Finish setup', resize: { columns: 90, rows: 30 }, send: '\u001b[B' },
    { wait: 'Add a model', resize: { columns: 50, rows: 16 }, send: '\u001b' },
    { wait: 'Setup closed. Configuration refreshed.', send: 'after resize\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored); assert.equal(result.backgroundQueries, 0);
  assert.match(result.output, /Project:.*project 日/);
  const raw = Buffer.from(result.raw, 'base64').toString(); assert.match(raw, /\x1b\[(?:38;2;0;90;156|38;5;31)m/);
});

test('real PTY: missing model opens actionable welcome and setup can be cancelled safely', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_') || key.startsWith('KIMI_MODEL_')) delete f.env[key];
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'Use /setup to connect a model', send: '/setup\r' },
    { wait: 'Finish setup', send: '\u001b' },
    { wait: 'Use /setup to connect a model', send: '/help\r' },
    { wait: 'KEYBOARD SHORTCUTS', send: '\u001b' },
    { wait: 'Use /setup to connect a model', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored); assert.equal(f.requests.length, 0);
});

test('real PTY: first-run setup connects a model and opens the conversation without restarting', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL_') || key.startsWith('KIMI_MODEL_')) delete f.env[key];
  const result = await terminal(f, [
    { wait: 'WORKSPACE TRUST', send: 'yes\r' },
    { wait: 'Use /setup to connect a model', send: '/setup\r' },
    { wait: 'Finish setup', send: '1\r' },
    { wait: 'Add provider · choose a service', send: '\r' },
    { wait: 'Provider ID', send: '\r' },
    { wait: 'API base URL', send: `http://127.0.0.1:${f.server.address().port}/v1\r` },
    { wait: 'API key · choose how to save it', send: '\r' },
    { wait: 'API key (input hidden)', send: 'test-only-key\r' },
    { wait: 'Select and add a model now', send: '\r' },
    { wait: 'Add model · openai', send: '\u001b[B\r' },
    { wait: 'Model ID from your provider', send: 'fixture-model\r' },
    { wait: 'Model alias', send: 'fixture\r' },
    { wait: 'Context limit in tokens', send: '128k\r' },
    { wait: 'Model capabilities', send: '\r' },
    { wait: 'Use this model by default?', send: '\r' },
    { wait: 'Finish setup', send: '\u001b' },
    { wait: 'Idle', send: 'first conversation after setup\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(JSON.stringify(f.requests.at(-1)), /first conversation after setup/);
  assert.match(result.output, /Ready to code/); assert.doesNotMatch(result.output, /test-only-key/);
});

test('real PTY: standalone setup detects a light background and restores the terminal', { timeout: 20000 }, async t => {
  const f = await fixture(); t.after(f.cleanup); delete f.env.NO_COLOR; f.env.FORCE_COLOR = '3';
  const result = await terminal(f, [
    { wait: 'Finish setup', send: '\u001b[B' },
    { wait: 'Add a model', send: '\u001b' },
  ], ['--setup'], { columns: 50, rows: 16, backgroundReply: 'rgb:ffff/ffff/ffff', splitBackgroundReply: true });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored); assert.equal(result.backgroundQueries, 1);
  assert.match(Buffer.from(result.raw, 'base64').toString(), /\x1b\[(?:38;2;0;90;156|38;5;31)m/);
});

test('real PTY: a late fragmented color reply cannot contaminate a hidden setup key', { timeout: 25000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Finish setup', send: '1\r' },
    { wait: 'Add provider · choose a service', send: '\r' },
    { wait: 'Provider ID', send: '\r' },
    { wait: 'API base URL', send: '\r' },
    { wait: 'API key · choose how to save it', send: '\r' },
    { wait: 'API key (input hidden)', send: '\u001b]11;rgb:ffff' },
    { wait: '', send: '/ffff/ffff\u0007private-setup-key\r' },
    { wait: 'Select and add a model now', send: '\u001b[B\r' },
    { wait: 'Finish setup', send: '\u001b' },
  ], ['--setup'], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.doesNotMatch(result.output, /private-setup-key|ffff/);
  assert.match(await readFile(join(f.home, 'config.toml'), 'utf8'), /api_key = "private-setup-key"/);
});

test('real PTY: settings aliases, reload and automatic-title preferences persist in a narrow terminal', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/config\r' },
    { wait: 'Settings', send: 'Automatic session titles\r' },
    { wait: 'TUI preferences saved and reloaded.', send: '/auto-title\r' },
    { wait: 'Automatic session titles: off', send: '/reload-tui\r' },
    { wait: 'TUI preferences reloaded.', send: '/new\r' },
    { wait: 'New session', send: '/auto-title\r' },
    { wait: 'Automatic session titles: off', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(await readFile(join(f.home, 'tui.toml'), 'utf8'), /auto_session_title = false/);
});

test('real PTY: experiments toggle and reload the session through either alias', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/experimental\r' },
    { wait: 'Experimental features', send: 'tool_select' },
    { wait: 'Search: tool_select', send: ' \r' },
    { wait: 'Experimental features saved.', send: 'hello after experiment reload\r' },
    { wait: 'Hello from the deterministic provider.', send: '/experiments\r' },
    { wait: 'Experimental features', send: '\x1b' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(await readFile(join(f.home, 'config.toml'), 'utf8'), /tool-select = true/);
});

test('real PTY: persistent approval defaults apply to new sessions and one-shot Swarm cancellation restores the command', { timeout: 35000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: '/swarm inspect this project\r' },
    { wait: 'Start Swarm with approvals on?', send: '\x1b' },
    { wait: 'Command restored.', send: '\x03' },
    { wait: 'Draft cleared.', send: '/permission\r' },
    { wait: 'Choose approval mode:', send: '\x1b[B\r' },
    { wait: 'Use this approval mode:', send: '\x1b[B\r' },
    { wait: 'saved as default for new sessions', send: '/new\r' },
    { wait: 'New session', send: '/permissions\r' },
    { wait: 'Permissions: yolo', send: '/swarm inspect this project\r' },
    { wait: 'Hello from the deterministic provider.', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(await readFile(join(f.home, 'config.toml'), 'utf8'), /default_permission_mode = "yolo"/);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('inspect this project')));
});

test('real PTY: interactive host capabilities deliver NotifyUser updates when the experiment is enabled', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.home, 'config.toml'), 'telemetry = false\n[experimental]\nnotify_user = true\n');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' },
    { wait: 'Idle', send: 'SCENARIO phase4-notify\r' },
    { wait: 'Notification fixture completed.', send: '/quit\r' },
  ], [], { columns: 50, rows: 16 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(result.output, /Updates · Test progress/);
  assert.match(result.output, /The interactive update reached the user/);
  assert.doesNotMatch(result.output, /Tool completed: Result/);
});
