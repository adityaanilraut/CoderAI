import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile, utimes } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { build } from 'esbuild';
import { fixture, cli, testRuntime } from './fixture.mjs';

const dir = await mkdtemp(resolve('node_modules/.phase2-'));
const entry = join(dir, 'sessions.mjs');
await build({
  stdin: { resolveDir: process.cwd(), contents: `
    export * from './packages/node-sdk/src/index.ts';
    export * from './packages/session-import/src/index.ts';
    export * from './packages/session-import/src/render.ts';
    export * from './apps/coderai/src/session-import/run-import.ts';
    export * from './apps/coderai/src/session-import/screen.ts';
    export * from './apps/coderai/src/session-operations.ts';
  ` },
  outfile: entry, bundle: true, platform: 'node', format: 'esm', target: 'node24',
  tsconfigRaw: { compilerOptions: { experimentalDecorators: true } },
  plugins: [{ name: 'engine-assets', setup(b) {
    b.onResolve({ filter: /^#/ }, async args => {
      let root = args.resolveDir;
      while (!existsSync(join(root, 'package.json'))) root = dirname(root);
      const path = args.path.startsWith('#human/') ? resolve('packages/agent-core-v2/src/human', args.path.slice(7)) : join(root, basename(root) === 'human' ? '' : 'src', args.path.slice(2));
      return b.resolve(path, { resolveDir: args.resolveDir, kind: args.kind });
    });
    b.onResolve({ filter: /\?raw$/ }, args => ({ path: resolve(args.resolveDir, args.path.slice(0, -4)), namespace: 'raw' }));
    b.onLoad({ filter: /.*/, namespace: 'raw' }, async args => ({ contents: await readFile(args.path, 'utf8'), loader: 'text' }));
    b.onResolve({ filter: /^[\w@][^:]*$/ }, args => {
      if (args.path.startsWith('@coderai/') || args.path.startsWith('#')) return;
      return { path: /^ajv\/dist\/(2019|2020)$/.test(args.path) ? args.path + '.js' : args.path, external: true };
    });
  } }],
});
const api = await import(entry);
api.configureTextBuildWorkerRuntime(resolve('dist/text-build-worker.mjs'));
after(() => rm(dir, { recursive: true, force: true }));
const lines = rows => rows.map(row => JSON.stringify(row)).join('\n') + '\n';
const now = Date.now();
const day = 86400000;
async function claude(path, cwd, age = 0) {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, lines([
    { type: 'user', uuid: 'u1', cwd, message: { role: 'user', content: 'First question' } },
    { type: 'assistant', uuid: 'a1', parentUuid: 'u1', cwd, message: { role: 'assistant', content: [{ type: 'text', text: 'First answer' }] } },
    { type: 'user', uuid: 'u2', parentUuid: 'a1', cwd, message: { role: 'user', content: 'Second question' } },
    { type: 'assistant', uuid: 'a2', parentUuid: 'u2', cwd, message: { role: 'assistant', content: [{ type: 'text', text: 'Second answer' }] } },
  ]));
  await utimes(path, new Date(now - age * day), new Date(now - age * day));
}
async function codex(path, cwd, id, source = 'cli', extra = {}) {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, lines([
    { type: 'session_meta', payload: { id, cwd, source, ...extra } },
    { type: 'turn_context', payload: { turn_id: 'turn-1', cwd } },
    { type: 'response_item', payload: { type: 'message', role: 'user', content: [{ type: 'input_text', text: 'Codex question' }] } },
    { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Codex answer' }] } },
  ]));
}
function homes(f) { return { claudeHome: join(f.root, 'claude'), codexHome: join(f.root, 'codex') }; }
const budget = { contextWindow: 131072, tokenBudget: 39000 };
const importOptions = f => ({ coderaiHome: f.home, ...budget, now });
const candidate = (path, cwd, sourceId, source = 'claude-code') => ({ path, cwd, sourceId, source, modifiedAt: now, sessionId: `ses_${sourceId}` });

async function harness(f) {
  const h = api.createCoderaiHarness({ homeDir: f.home, configPath: join(f.home, 'config.toml'), identity: { productName: 'coderai-test', version: '0.1.0', platform: 'kimi_code_cli' } });
  await h.setConfig({ providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } }, models: { local: { provider: 'local', model: 'fixture-model', maxContextSize: 131072 } }, defaultModel: 'local', watch: { enabled: false }, database: { search: false } });
  return h;
}

test('discovery uses recent windows, latest Codex database and descendant rollouts; imports are idempotent', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const home = homes(f);
  const recent = join(home.claudeHome, 'projects', 'p', 'recent.jsonl');
  await claude(recent, f.cwd);
  await claude(join(home.claudeHome, 'projects', 'p', 'older.jsonl'), f.cwd, 20);
  await claude(join(home.claudeHome, 'projects', 'p', 'expired.jsonl'), f.cwd, 35);
  await claude(join(home.claudeHome, 'projects', 'p', 'missing.jsonl'), join(f.root, 'missing'));
  const rollout = join(home.codexHome, 'sessions', '2026', 'rollout-parent.jsonl');
  const child = join(dirname(rollout), 'rollout-child.jsonl');
  const grandchild = join(dirname(rollout), 'rollout-grandchild.jsonl');
  await codex(rollout, f.cwd, 'parent');
  await codex(child, f.cwd, 'child', { subagent: { thread_spawn: { parent_thread_id: 'parent' } } });
  await codex(grandchild, f.cwd, 'grandchild', { subagent: { thread_spawn: { parent_thread_id: 'child' } } });
  const database = join(home.codexHome, 'state_5.sqlite');
  const db = new DatabaseSync(database);
  db.exec('CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT)');
  db.prepare('INSERT INTO threads VALUES (?, ?)').run('parent', 'SQLite thread title'); db.close();
  await writeFile(join(home.codexHome, 'state_2.sqlite'), 'old database');
  const scan = await api.scanImportableSessions({ coderaiHome: f.home, ...home, now: now + 100 });
  assert.equal(scan.missingFolder, 1);
  assert.equal(api.candidatesWithin(scan, 15, now).length, 2);
  assert.equal(api.candidatesWithin(scan, 30, now).length, 3);
  const parent = scan.candidates.find(item => item.sourceId === 'parent');
  assert.deepEqual(parent.subagentPaths, [child, grandchild].sort());
  assert.equal(parent.stateDatabasePath, database);
  const report = await api.importSessions(scan.candidates, importOptions(f));
  assert.equal(report.imported.length, 3, JSON.stringify(report));
  const sessionDir = api.importedSessionDir(f.home, f.cwd, parent.sessionId);
  const state = JSON.parse(await readFile(join(sessionDir, 'state.json'), 'utf8'));
  assert.equal(state.title, 'SQLite thread title');
  assert.equal(state.titleKind, 'generated');
  assert.deepEqual(state.custom, { imported_from: 'codex', imported_source_id: 'parent', imported_source_path: rollout, imported_at: new Date(now).toISOString() });
  assert.equal((await readdir(join(sessionDir, 'attachments', 'source', 'subagents'))).length, 2);
  assert.equal(await readFile(join(sessionDir, 'attachments', 'source', basename(rollout)), 'utf8'), await readFile(rollout, 'utf8'));
  const repeated = await api.importSessions(scan.candidates, importOptions(f));
  assert.equal(repeated.alreadyImported, 3);
  const scannedAgain = await api.scanImportableSessions({ coderaiHome: f.home, ...home, now });
  assert.equal(scannedAgain.alreadyImported, 3);
  assert.equal(scannedAgain.candidates.length, 0);
  assert.equal((await readFile(join(f.home, 'session_index.jsonl'), 'utf8')).trim().split('\n').length, 3);
});

test('structured history resumes, replays and continues after imported turns, preserving metadata and attachments', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const source = join(f.root, 'session.jsonl'); await claude(source, f.cwd);
  const h = await harness(f); t.after(() => h.close());
  await h.trustWorkspace(f.cwd);
  // Start the index before import: a lookup must refresh the running index.
  assert.equal((await h.listSessions()).length, 0);
  const report = await api.importSessions([candidate(source, f.cwd, 'structured')], importOptions(f));
  assert.deepEqual(report.imported, ['ses_structured']);
  assert.equal((await h.listSessions({ sessionId: 'ses_structured' })).length, 1);
  const session = await h.resumeSession({ id: 'ses_structured' });
  const history = (await session.getContext()).history;
  assert.deepEqual(history.map(message => message.role), ['user', 'assistant', 'user', 'assistant']);
  assert.match(JSON.stringify(history[1]), /First answer/);
  assert.match(JSON.stringify(history[2]), /Second question/);
  assert.ok(session.getResumeState().agents.main.replay.length >= 4);
  const events = [];
  let off;
  const completed = new Promise((resolveTurn, reject) => {
    const timer = setTimeout(() => reject(new Error('Continuation timed out')), 12000);
    off = session.onEvent(event => { events.push(event); if (event.type === 'turn.ended') { clearTimeout(timer); resolveTurn(); } });
  });
  await session.prompt('Continue the imported work'); await completed; off();
  const turn = events.find(event => event.type === 'turn.started');
  assert.equal(turn.turnId, 2, JSON.stringify(turn));
  assert.equal(events.find(event => event.type === 'turn.ended').reason, 'completed');
  assert.match(JSON.stringify(f.requests.at(-1).messages), /First answer/);
  const sessionDir = api.importedSessionDir(f.home, f.cwd, 'ses_structured');
  const state = JSON.parse(await readFile(join(sessionDir, 'state.json'), 'utf8'));
  assert.equal(state.title, 'First question');
  assert.equal(state.custom.imported_from, 'claude-code');
  await session.close();
});

test('imports clean interrupted writes, classify failures, skip missing folders and stop between sessions', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const empty = join(f.root, 'empty.jsonl'); await writeFile(empty, '');
  const unsupported = join(f.root, 'forked.jsonl'); await codex(unsupported, f.cwd, 'forked', 'cli', { history_base: { path: 'old.jsonl' } });
  const good = join(f.root, 'good.jsonl'); await claude(good, f.cwd);
  const pending = api.importedSessionDir(f.home, f.cwd, 'ses_good');
  await mkdir(pending, { recursive: true }); await writeFile(join(pending, 'leftover'), 'interrupted');
  const report = await api.importSessions([
    candidate(empty, f.cwd, 'empty'), candidate(unsupported, f.cwd, 'forked', 'codex'),
    candidate(good, join(f.root, 'gone'), 'missing'), candidate(join(f.root, 'absent'), f.cwd, 'absent'), candidate(good, f.cwd, 'good'),
  ], importOptions(f));
  assert.equal(report.empty, 1); assert.equal(report.unsupported, 1); assert.equal(report.missingFolder, 1); assert.equal(report.failed.length, 1);
  assert.deepEqual(report.imported, ['ses_good']);
  assert.equal(existsSync(join(pending, 'leftover')), false);
  for (const id of ['empty', 'forked', 'absent']) assert.equal(existsSync(api.importedSessionDir(f.home, f.cwd, `ses_${id}`)), false);
  const stopped = await api.importSessions([candidate(good, f.cwd, 'stop')], { ...importOptions(f), shouldStop: () => true });
  assert.equal(stopped.stopped, true); assert.equal(stopped.imported.length, 0);
  assert.match(api.importedSessionId({ source: 'codex', sourceId: '../../unsafe' }), /^ses_[a-f0-9]{32}$/);
});

test('import budgets apply model overrides and structured truncation stays bounded', async t => {
  const config = { defaultModel: 'm', models: { m: { provider: 'p', model: 'm', maxContextSize: 200000, overrides: { maxInputSize: 100000 } } } };
  assert.deepEqual(api.importBudget(config), { contextWindow: 100000, tokenBudget: 16384 });
  const history = { source: 'codex', sourcePath: '/source.jsonl', sourceId: 'x', metadata: {}, stats: {}, imageReferences: [], turns: Array.from({ length: 20 }, (_, i) => ({ id: `${i}`, user: `Question ${i}`, steps: [{ kind: 'assistant', text: `Answer ${i} ` + 'code '.repeat(800) }] })) };
  const result = api.renderHistory(history, { historyPath: '/attachments/history.jsonl', sourceCopy: { directory: '/attachments/source', folder: false, subagentRollouts: 0 } }, 3000, []);
  assert.ok(result.truncated); assert.ok(result.stats.omittedTurns > 0);
  assert.ok(result.stats.estimatedTokens <= 3000);
  assert.ok(result.messages.some(message => message.role === 'assistant'));
  assert.match(JSON.stringify(result.messages), /Answer 19/);
});

test('import screen cancellation keeps the current import alive until it finishes and reports retained sessions', async () => {
  const c = candidate('/tmp/source', '/tmp/project', 'screen');
  let finishRun, hooks, closed = 0;
  const report = { imported: ['ses_screen'], alreadyImported: 0, missingFolder: 0, inUse: 0, empty: 0, unsupported: 0, failed: [], stopped: true };
  const screen = new api.ImportSessionsScreenComponent({ now, scan: async () => ({ candidates: [c], alreadyImported: 0, missingFolder: 0 }),
    run: async (candidates, passedHooks) => { hooks = passedHooks; return new Promise(resolve => { finishRun = resolve; }); },
    onClose: value => { assert.equal(value, report); closed++; },
  });
  await Promise.resolve();
  screen.handleInput('\r');
  assert.equal(hooks.shouldStop(), false);
  screen.handleInput('\x1b');
  assert.equal(hooks.shouldStop(), true);
  assert.equal(closed, 0);
  assert.match(screen.render(100).join('\n'), /Stopping after the current session/);
  finishRun(report);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(screen.render(100).join('\n'), /Import stopped. Imported 1 session/);
  screen.handleInput('\r'); screen.handleInput('\r'); assert.equal(closed, 1);
  screen.dispose();
});

test('import screen strips terminal controls from discovered workspaces and failures', async () => {
  const c = candidate('/tmp/source', '/tmp/project\x1b]52;c;INJECTED\x07', 'screen');
  let finishRun, progress;
  const screen = new api.ImportSessionsScreenComponent({ now, scan: async () => ({ candidates: [c], alreadyImported: 0, missingFolder: 0 }),
    run: async (candidates, hooks) => { progress = hooks; return new Promise(resolve => { finishRun = resolve; }); }, onClose: () => {},
  });
  await Promise.resolve(); screen.handleInput('\r');
  progress.onProgress({ index: 0, total: 1, candidate: c });
  assert.doesNotMatch(screen.render(100).join('\n'), /INJECTED|\x07/);
  finishRun({ imported: [], alreadyImported: 0, missingFolder: 0, inUse: 0, empty: 0, unsupported: 0, failed: [{ path: '/tmp/source\x1b]52;c;INJECTED\x07', message: 'failure\x1b]52;c;INJECTED\x07' }], stopped: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.doesNotMatch(screen.render(100).join('\n'), /INJECTED|\x07/);
  screen.dispose();
});

const terminal = (f, actions, args = []) => new Promise((done, reject) => {
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = '', err = ''; child.stdout.on('data', c => out += c); child.stderr.on('data', c => err += c);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, node: testRuntime, cli, env: { ...f.env, TERM: 'xterm-256color' }, actions, args }));
  child.on('error', reject); child.on('close', code => code === 0 ? done(JSON.parse(out)) : reject(new Error(err + out)));
});

test('CLI session options, rename/delete, turn forks, Markdown and ZIP exports round trip', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const source = join(f.root, 'session.jsonl'); await claude(source, f.cwd);
  const imported = await f.run(['--trust', 'sessions', 'import', 'claude-code', source]);
  assert.equal(imported.code, 0, imported.stderr);
  const id = imported.stdout.trim().split(/\s/)[0];
  const list = await f.run(['sessions', 'list', '--json', '--limit', '1']);
  assert.equal(list.code, 0, list.stderr); assert.equal(JSON.parse(list.stdout)[0].id, id);
  const renamed = await f.run(['sessions', 'rename', id, 'New title']); assert.equal(renamed.code, 0, renamed.stderr);
  const forked = await f.run(['sessions', 'fork', id, '--turn', '0', '--title', 'Only first turn']);
  assert.equal(forked.code, 0, forked.stderr); const forkId = forked.stdout.trim();
  const exported = await f.run(['sessions', 'export', forkId, 'fork.md']); assert.equal(exported.code, 0, exported.stderr);
  const markdown = await readFile(join(f.cwd, 'fork.md'), 'utf8'); assert.match(markdown, /First answer/); assert.doesNotMatch(markdown, /Second answer/);
  const zip = await f.run(['sessions', 'export', id, 'session.zip', '--zip']); assert.equal(zip.code, 0, zip.stderr);
  assert.equal((await readFile(join(f.cwd, 'session.zip'))).subarray(0, 2).toString(), 'PK');
  for (const value of ['0', '-1', '1x', '1.5']) { const invalid = await f.run(['sessions', '--limit', value]); assert.equal(invalid.code, 1); assert.match(invalid.stderr, /positive integer/); }
  const other = join(f.root, 'other'); await mkdir(other);
  let scoped = await f.run(['--cwd', other, 'sessions', '--json']); assert.deepEqual(JSON.parse(scoped.stdout), []);
  scoped = await f.run(['--cwd', other, 'sessions', '--all', '--json']); assert.equal(JSON.parse(scoped.stdout).length, 2);
  // Archive on disk between CLI processes, as external session management does.
  const statePath = join(JSON.parse(list.stdout)[0].sessionDir, 'state.json');
  const state = JSON.parse(await readFile(statePath, 'utf8')); await writeFile(statePath, JSON.stringify({ ...state, archived: true }));
  const active = await f.run(['sessions', '--json']); assert.ok(!JSON.parse(active.stdout).some(row => row.id === id));
  const archived = await f.run(['sessions', 'list', '--archived', '--json']); assert.ok(JSON.parse(archived.stdout).some(row => row.id === id && row.archived));
  const removed = await f.run(['sessions', 'delete', forkId]); assert.equal(removed.code, 0, removed.stderr);
  const remaining = await f.run(['sessions', '--all', '--archived', '--json']); assert.ok(!JSON.parse(remaining.stdout).some(row => row.id === forkId));
  const nonterminal = await f.run(['import-sessions']); assert.equal(nonterminal.code, 1); assert.match(nonterminal.stderr, /interactive terminal/);
});

test('real PTY: standalone and slash import pages discover and import locally', { timeout: 45000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const home = homes(f); Object.assign(f.env, { CLAUDE_CONFIG_DIR: home.claudeHome, CODEX_HOME: home.codexHome });
  await claude(join(home.claudeHome, 'projects', 'p', 'standalone.jsonl'), f.cwd);
  const standalone = await terminal(f, [
    { wait: 'Last 15 days', send: '\r' }, { wait: 'Imported 1 session.', send: '\r' },
  ], ['import-sessions']);
  assert.equal(standalone.code, 0, standalone.output); assert.ok(standalone.terminalRestored);
  await claude(join(home.claudeHome, 'projects', 'p', 'slash.jsonl'), f.cwd);
  const slash = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: '/import\r' },
    { wait: 'Last 15 days', send: '\r' }, { wait: 'Imported 1 session.', send: '\r' },
    { wait: 'Find them with /sessions (Ctrl+A', send: '/fork 0\r' },
    { wait: 'was not found in session', send: '/quit\r' },
  ]);
  assert.equal(slash.code, 0, slash.output); assert.ok(slash.terminalRestored);
  const list = await f.run(['sessions', '--all', '--json']);
  assert.ok(JSON.parse(list.stdout).some(row => row.id === 'ses_slash'));
});

test('real PTY: interactive --attach is staged for the first prompt', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup); f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  await writeFile(join(f.cwd, 'image.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aM1sAAAAASUVORK5CYII=', 'base64'));
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: '/attach list\r' },
    { wait: 'Attachments for the next prompt:', send: 'Explain this image\r' },
    { wait: 'Hello from the deterministic provider.', send: '/attach list\r' },
    { wait: 'No attachments staged.', send: '/quit\r' },
  ], ['--attach', 'image.png']);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(JSON.stringify(f.requests.at(-1).messages), /data:image\/png;base64,/);
});
