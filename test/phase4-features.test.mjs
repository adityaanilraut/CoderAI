import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { build } from 'esbuild';
import { fixture } from './fixture.mjs';
import { loadTuiConfig, saveTuiConfig, saveAutoSessionTitle } from '../apps/coderai/src/tui/config.ts';
import { ExperimentsDialog, showExperiments } from '../apps/coderai/src/tui/experiments.ts';
import { startSwarm, TOWER_TEARDOWN_PROMPT } from '../apps/coderai/src/tui/modes.ts';
import { UpdatesPanel } from '../apps/coderai/src/tui/updates.ts';
import { Markdown, setRenderLatex } from '../apps/coderai/src/tui/markdown.ts';
import { markdownTheme } from '../apps/coderai/src/tui/theme.ts';
import { runStatusLineCommand } from '../apps/coderai/src/tui/status-line-command.ts';
import { headless } from '../apps/coderai/src/headless.ts';
import { visibleWidth, stripTerminalSequences } from '../packages/pi-tui/src/utils.ts';
const dir = await mkdtemp(resolve('node_modules/.phase4-'));
const entry = join(dir, 'features.mjs');
await build({
  stdin: { resolveDir: process.cwd(), contents: `
    export * from './packages/node-sdk/src/index.ts';
    export * from './apps/coderai/src/runtime.ts';
    export * from './apps/coderai/src/tui/commands.ts';
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

const event = (type, data = {}) => ({ type, agentId: 'main', sessionId: 's', ...data });
const plain = component => component.render(90).map(stripTerminalSequences).join('\n');
async function temp(t) { const home = await mkdtemp(join(tmpdir(), 'coderai-phase4-')); t.after(() => rm(home, { recursive: true, force: true })); return home; }
function commandHost(choices = []) {
  const calls = [], messages = [];
  const status = { permission: 'manual', swarmMode: false, towerMode: false };
  const config = { providers: {}, models: { one: { provider: 'p', model: 'model' }, two: { provider: 'p', model: 'other' } }, secondaryModel: { models: { one: 'first' }, defaultModel: 'one' } };
  const session = { id: 's', getStatus: async () => ({ ...status }), setPermission: async mode => { status.permission = mode; calls.push(['permission', mode]); }, setSwarmMode: async (...args) => { status.swarmMode = args[0]; calls.push(['swarm', ...args]); }, setTowerMode: async (...args) => { status.towerMode = args[0]; calls.push(['tower', ...args]); } };
  const runtime = { session, validateModel: async alias => calls.push(['validate', alias]), harness: { getConfig: async () => config, setConfig: async patch => calls.push(['config', patch]) } };
  const view = { choose: async () => choices.shift() ?? '', chooseSearchable: async () => choices.shift() ?? '', message: text => messages.push(text), notice: text => messages.push(text), editor: { setText: text => calls.push(['draft', text]) } };
  return { runtime, view, status, config, calls, messages };
}

test('TUI preferences round-trip every Phase 4 field, merge concurrent saves and preserve Phase 3/unknown keys', async t => {
  const home = await temp(t);
  await writeFile(join(home, 'tui.toml'), 'future = "preserved"\nauto_session_title = false\n[editor]\nfuture = 1\n');
  await Promise.all([
    saveTuiConfig(home, { theme: 'light', tui_mode: 'fullscreen', render_latex: false, disable_paste_burst: true }),
    saveTuiConfig(home, { editor: { command: 'vim -f' }, notifications: { enabled: false, notification_condition: 'always' }, markdown: { mermaid: 'off' }, status_line: { items: ['cwd', 'model'], command: 'echo custom' } }),
    saveAutoSessionTitle(home, true),
  ]);
  assert.deepEqual(await loadTuiConfig(home), { theme: 'light', tuiMode: 'fullscreen', renderLatex: false, disablePasteBurst: true, autoSessionTitle: true, editorCommand: 'vim -f', notifications: { enabled: false, condition: 'always' }, markdown: { mermaid: 'off' }, statusLine: { items: ['cwd', 'model'], command: 'echo custom' } });
  assert.match(await readFile(join(home, 'tui.toml'), 'utf8'), /future = "preserved"/);
  await writeFile(join(home, 'tui.toml'), 'auto_session_title = "no"');
  await assert.rejects(saveAutoSessionTitle(home, true), /Invalid TUI config/);
  assert.equal(await readFile(join(home, 'tui.toml'), 'utf8'), 'auto_session_title = "no"');
});

test('unknown preference values warn and legacy theme migrates without competing title paths', async t => {
  const home = await temp(t), warnings = [];
  await writeFile(join(home, 'theme'), 'light\n');
  assert.equal((await loadTuiConfig(home)).theme, 'light');
  await saveTuiConfig(home, { theme: 'dark', tui_mode: 'bad', markdown: { mermaid: 'bad' }, status_line: { items: ['model', 'unknown', 'model'] } });
  const config = await loadTuiConfig(home, message => warnings.push(message));
  assert.equal(config.theme, 'dark'); assert.equal(config.tuiMode, 'regular'); assert.deepEqual(config.statusLine.items, ['model']); assert.equal(warnings.length, 3);
});

test('experiments respect environment locks, reverse draft toggles, cancel, and stay within narrow terminal bounds', () => {
  const features = [{ id: 'tower', title: 'Tower', description: 'Long description '.repeat(30), enabled: false, source: 'default', env: 'FLAG' }, { id: 'notify_user', title: 'Updates', description: 'Updates', enabled: true, source: 'env', env: 'LOCK' }];
  let result;
  const dialog = new ExperimentsDialog(features, () => 14, answer => result = answer, () => {});
  assert.match(plain(dialog), /Enter apply/); assert.doesNotMatch(plain(dialog), /Enter select/);
  dialog.handleInput(' '); assert.equal(dialog.changes.get('tower'), true);
  dialog.handleInput(' '); assert.equal(dialog.changes.size, 0);
  dialog.handleInput('\x1b[B'); dialog.handleInput(' '); assert.equal(dialog.changes.size, 0);
  for (const width of [0, 1, 12, 40, 90]) assert.ok(dialog.render(width).every(line => visibleWidth(line) <= width));
  dialog.handleInput('\x1b'); assert.equal(result, '');
});

test('experimental apply persists engine changes and reloads the existing session without clearing the view', async () => {
  const calls = [];
  const runtime = { harness: { getExperimentalFeatures: async () => [{ id: 'tool_select', title: 'Tool select', description: '', enabled: false, source: 'default' }], setConfig: async patch => calls.push(patch) }, session: { reloadSession: async () => calls.push('reload') } };
  const view = { tui: { terminal: { rows: 20 }, requestRender() {} }, async showDialog(create) { const dialog = create(() => {}); dialog.handleInput(' '); return 'apply'; }, message() {}, notice() {} };
  await showExperiments(runtime, view);
  assert.deepEqual(calls, [{ experimental: { tool_select: true } }, 'reload']);
});

test('Swarm permission choices and cancellation preserve command and only dispatch task mode after a choice', async () => {
  for (const choice of ['', 'manual', 'yolo', 'auto']) {
    const h = commandHost([choice]);
    assert.equal(await startSwarm(h.runtime, h.view, '/swarm do work', 'task'), !!choice);
    if (!choice) assert.deepEqual(h.calls, [['draft', '/swarm do work']]);
    else { assert.deepEqual(h.calls.at(-1), ['swarm', true, 'task']); assert.equal(h.status.permission, choice); }
  }
  const h = commandHost(); h.status.permission = 'auto'; h.status.swarmMode = true;
  await startSwarm(h.runtime, h.view, '/swarm another task', 'task'); assert.deepEqual(h.calls, []);
});

test('Tower accepts direct base branches and refuses to report success if engine refuses activation', async () => {
  const h = commandHost(); await api.command(h.runtime, h.view, '/tower main'); assert.deepEqual(h.calls, [['tower', true, 'main']]);
  h.runtime.session.setTowerMode = async () => {}; h.status.towerMode = false;
  await assert.rejects(api.command(h.runtime, h.view, '/tower on'), /could not be changed/);
  assert.equal(await api.command(h.runtime, h.view, '/tower teardown'), false); assert.match(TOWER_TEARDOWN_PROMPT, /TowerTeardown.*dirty/);
});

test('permission defaults require a scope choice; secondary aliases preserve and extend the model pool', async () => {
  const cancel = commandHost(['yolo', '']); await api.command(cancel.runtime, cancel.view, '/permission'); assert.deepEqual(cancel.calls, []);
  const h = commandHost(['yolo', 'default']); await api.command(h.runtime, h.view, '/permission');
  assert.deepEqual(h.calls, [['config', { defaultPermissionMode: 'yolo' }], ['permission', 'yolo']]);
  await api.command(h.runtime, h.view, '/subagent-model two');
  assert.deepEqual(h.calls.at(-1), ['config', { secondaryModel: { defaultModel: 'two', models: { one: 'first', two: '' } } }]);
  await assert.rejects(api.command(h.runtime, h.view, '/secondary-model primary'), /reserved/);
});

test('NotifyUser commits successful deliveries only, handles children and resets on session switch', () => {
  const panel = new UpdatesPanel(() => 16);
  const start = (id, agentId = 'main') => event('tool.call.started', { agentId, toolCallId: id, name: 'NotifyUser', args: { title: 'Progress', message: 'Safe\x1b]52;c;BAD\x07 update' } });
  panel.handle(start('1')); assert.equal(plain(panel), '');
  panel.handle(event('tool.result', { toolCallId: '1', output: 'Notifications are disabled; the update was not displayed.' })); assert.equal(plain(panel), '');
  panel.handle(start('2', 'child')); assert.equal(panel.handle(event('tool.result', { agentId: 'child', toolCallId: '2', output: 'Update shown to the user.' })), true);
  assert.match(plain(panel), /Progress/); assert.doesNotMatch(plain(panel), /BAD/);
  for (const width of [0, 1, 12, 40]) assert.ok(panel.render(width).every(line => visibleWidth(line) <= width));
  panel.clear(); assert.equal(plain(panel), '');
});

test('LaTeX preferences update already-rendered transcript Markdown and custom status commands are bounded', async () => {
  const markdown = new Markdown('$\\alpha$', 0, 0, markdownTheme);
  setRenderLatex(true); assert.match(plain(markdown), /α/);
  setRenderLatex(false); assert.match(plain(markdown), /\$\\alpha\$/); setRenderLatex(true);
  assert.equal(await runStatusLineCommand('read payload; printf "first\\nsecond"', {}, 500), 'first');
  assert.equal(await runStatusLineCommand('exit 1', {}, 500), null);
  const before = Date.now(); assert.equal(await runStatusLineCommand('sleep 10', {}, 50), null); assert.ok(Date.now() - before < 2000);
});

function runHost(policy = ['finish']) {
  const listeners = new Set(), calls = [];
  const emit = e => { for (const listener of [...listeners]) listener(e); };
  const session = { onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); }, setApprovalHandler() {}, setQuestionHandler() {}, async prompt() { emit(event('turn.started')); emit(event('turn.ended', { reason: 'completed' })); }, async handlePrintMainTurnCompleted() { calls.push('policy'); return policy.shift() ?? 'finish'; } };
  const runtime = { session, harness: { getConfig: async () => ({ background: { printWaitCeilingS: 1 } }) } };
  return { runtime, emit, calls, listeners };
}

test('main-turn completion follows print policy, streams steered turns and releases listeners on cancellation', async () => {
  const h = runHost(['continue', 'finish']);
  const result = api.Runtime.prototype.run.call(h.runtime, 'task', () => {}, () => {}, undefined, undefined, false, false, true);
  await new Promise(resolve => setTimeout(resolve, 10)); assert.equal(h.calls.length, 1);
  h.emit(event('turn.started')); h.emit(event('turn.ended', { reason: 'completed' }));
  assert.equal(await result, 'completed'); assert.equal(h.calls.length, 2); assert.equal(h.listeners.size, 0);
  const cancelled = runHost(['continue']); const abort = new AbortController();
  const waiting = api.Runtime.prototype.run.call(cancelled.runtime, 'task', () => {}, () => {}, undefined, undefined, false, false, true, abort.signal);
  await new Promise(resolve => setTimeout(resolve, 10)); abort.abort(); assert.equal(await waiting, 'cancelled'); assert.equal(cancelled.listeners.size, 0);
});

test('headless skills, plugins and lifecycle controls use engine dispatch without sending slash commands as model text', async () => {
  const calls = [];
  const session = { id: 's', workDir: '/test', setStaleReloadHandler() {}, getSessionWarnings: async () => [], listSkills: async () => [{ name: 'review', scopes: ['tui'] }], listPluginCommands: async () => [{ pluginId: 'local', name: 'check' }], promptWithSkills: async (...args) => calls.push(['skill', ...args]), activatePluginCommand: async (...args) => calls.push(['plugin', ...args]), getGoal: async () => ({ goal: { objective: 'Work', status: 'paused', turnsUsed: 1, tokensUsed: 2 } }), pauseGoal: async () => calls.push('pause'), cancelGoal: async () => calls.push('cancel') };
  const runtime = { session, options: {}, open: async () => session, async run(prompt, emit, approve, question, dispatch, follow, created, policy) { calls.push(['run', prompt, policy]); if (dispatch) await dispatch(session); return 'completed'; } };
  const oldOut = process.stdout.write, oldErr = process.stderr.write;
  try {
    process.stdout.write = process.stderr.write = () => true;
    assert.equal(await headless(runtime, '/skill:review file.ts', true), 0);
    assert.deepEqual(calls.at(-1), ['skill', 'file.ts', [{ name: 'review', args: 'file.ts' }]]);
    assert.equal(await headless(runtime, '/local:check file.ts', true), 0); assert.deepEqual(calls.at(-1), ['plugin', 'local', 'check', 'file.ts']);
    const count = calls.filter(call => Array.isArray(call) && call[0] === 'run').length;
    for (const control of ['status', 'pause', 'cancel']) assert.equal(await headless(runtime, `/goal ${control}`, true), 0);
    assert.equal(calls.filter(call => Array.isArray(call) && call[0] === 'run').length, count);
    assert.ok(calls.includes('pause')); assert.ok(calls.includes('cancel'));
    await assert.rejects(headless(runtime, '/skill:missing', true), /unavailable/);
  } finally { process.stdout.write = oldOut; process.stderr.write = oldErr; }
});

test('real engine profile inspection and cron deletion persist through reload with the same session', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const runtime = new api.Runtime({ home: f.home, cwd: f.cwd, trust: true, interactive: true }); t.after(() => runtime.close());
  await runtime.harness.setConfig({ providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } }, models: { local: { provider: 'local', model: 'fixture-model', maxContextSize: 131072 } }, defaultModel: 'local', watch: { enabled: false }, database: { search: false } });
  await runtime.trust(async () => true); const session = await runtime.open();
  assert.ok((await runtime.harness.listAgentProfiles(session.id)).some(profile => profile.name === 'agent'));
  assert.equal(await runtime.run('SCENARIO phase4-cron', () => {}, () => ({ decision: 'approved' })), 'completed');
  const { tasks } = await session.getCronTasks(); assert.equal(tasks.length, 1); assert.equal(tasks[0].recurring, false);
  assert.equal(await session.deleteCronTask('missing'), false); assert.equal(await session.deleteCronTask(tasks[0].id), true);
  await session.reloadSession(); assert.deepEqual((await session.getCronTasks()).tasks, []);
});

test('real CLI: skill activation and default print steer dispatch through the engine', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const { mkdir } = await import('node:fs/promises');
  const path = join(f.cwd, '.coderai', 'skills', 'phase4-review'); await mkdir(path, { recursive: true });
  await writeFile(join(path, 'SKILL.md'), '---\nname: phase4-review\ndescription: Review the requested input\n---\nPHASE4_SKILL_INSTRUCTIONS\n');
  const skill = await f.run(['--trust', '-p', '/skill:phase4-review inspect input.txt', '--json']);
  assert.equal(skill.code, 0, skill.stderr);
  const events = skill.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.ok(events.some(event => event.type === 'skill.activated' && event.skillName === 'phase4-review'));
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('PHASE4_SKILL_INSTRUCTIONS')));
  const steer = await f.run(['--trust', '--yolo', '-p', 'SCENARIO background-short', '--json']);
  assert.equal(steer.code, 0, steer.stderr);
  const streamed = steer.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.ok(streamed.filter(event => event.type === 'turn.ended' && event.agentId === 'main').length >= 2);
  assert.equal(streamed.at(-1).exitCode, 0);
});

test('real engine: all requested experiments are inspectable and compaction flag gates entry without affecting defaults', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const runtime = new api.Runtime({ home: f.home, cwd: f.cwd, trust: true }); t.after(() => runtime.close());
  await runtime.harness.setConfig({ providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } }, models: { local: { provider: 'local', model: 'fixture-model', maxContextSize: 131072 } }, defaultModel: 'local', watch: { enabled: false }, database: { search: false } });
  const features = await runtime.harness.getExperimentalFeatures();
  for (const id of ['tower', 'notify_user', 'tool-select', 'subagent_fork', 'wait_for', 'full_compaction']) assert.ok(features.some(feature => feature.id === id), id);
  assert.equal(features.find(feature => feature.id === 'full_compaction').enabled, true);
  await runtime.trust(async () => true); const session = await runtime.open();
  await runtime.harness.setConfig({ experimental: { full_compaction: false } }); await session.reloadSession();
  await assert.rejects(session.compact(), /Full compaction is disabled/);
  await runtime.harness.setConfig({ experimental: { full_compaction: true } }); await session.reloadSession();
  await assert.rejects(session.compact(), /No messages/);
});

test('real CLI: deterministic goal lifecycle controls persist and resume follows completion', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const runtime = new api.Runtime({ home: f.home, cwd: f.cwd, trust: true });
  await runtime.harness.setConfig({ providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } }, models: { local: { provider: 'local', model: 'fixture-model', maxContextSize: 131072 } }, defaultModel: 'local', watch: { enabled: false }, database: { search: false } });
  await runtime.trust(async () => true); const session = await runtime.open();
  const id = session.id;
  const cancellation = await runtime.harness.createSession({ workDir: f.cwd });
  const cancelId = cancellation.id;
  await cancellation.createGoal({ objective: 'Cancel this queued goal' }); await cancellation.pauseGoal(); await cancellation.close();
  try { await session.createGoal({ objective: 'SCENARIO goal' }); await session.pauseGoal(); }
  finally { await runtime.close(); }
  const before = f.requests.length;
  const status = await f.run(['--trust', '--resume', id, '-p', '/goal status', '--json']);
  assert.equal(status.code, 0, status.stderr); assert.equal(JSON.parse(status.stdout).goal.status, 'paused');
  assert.equal(f.requests.length, before);
  const pause = await f.run(['--trust', '--resume', id, '-p', '/goal pause', '--json']);
  assert.equal(pause.code, 0, pause.stderr); assert.equal(JSON.parse(pause.stdout).goal.status, 'paused');
  assert.equal(f.requests.length, before);
  const resume = await f.run(['--trust', '--resume', id, '-p', '/goal resume', '--json']);
  assert.equal(resume.code, 0, resume.stderr);
  const events = resume.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).goalStatus, 'complete');
  const cancel = await f.run(['--trust', '--resume', cancelId, '-p', '/goal cancel', '--json']);
  assert.equal(cancel.code, 0, cancel.stderr); assert.equal(JSON.parse(cancel.stdout).goal, null);
});

test('real CLI: headless runs do not advertise NotifyUser even when its flag is enabled', async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.home, 'config.toml'), 'telemetry = false\n[experimental]\nnotify_user = true\n');
  const result = await f.run(['--trust', '-p', 'Inspect available tools.']);
  assert.equal(result.code, 0, result.stderr);
  assert.ok(f.requests.length); assert.ok(!f.requests.at(-1).tools.some(tool => tool.function.name === 'NotifyUser'));
});
