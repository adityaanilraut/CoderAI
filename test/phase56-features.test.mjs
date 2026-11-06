import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile, symlink } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { build } from 'esbuild';
import { spawn } from 'node:child_process';
import { fixture, cli, testRuntime } from './fixture.mjs';
import { CompletionProvider } from '../apps/coderai/src/tui/completion.ts';
import { QuestionDialog } from '../apps/coderai/src/tui/questions.ts';
import { ChoiceDialog } from '../apps/coderai/src/tui/dialogs.ts';
import { Markdown, setMermaidMode } from '../apps/coderai/src/tui/markdown.ts';
import { markdownTheme, setTheme } from '../apps/coderai/src/tui/theme.ts';
import { installCapabilityAndWait, capabilityText } from '../apps/coderai/src/plugin-operations.ts';
import { inlineSkillActivations } from '../apps/coderai/src/inline-skills.ts';
import { TuiAltScreen, VStack, ScrollView, Text } from '../packages/pi-tui/src/index.ts';
import { visibleWidth, stripTerminalSequences } from '../packages/pi-tui/src/utils.ts';
const dir = await mkdtemp(resolve('node_modules/.phase56-'));
const entry = join(dir, 'features.mjs');
await build({
  stdin: { resolveDir: process.cwd(), contents: `
    export * from './packages/node-sdk/src/index.ts';
    export * from './apps/coderai/src/runtime.ts';
    export * from './apps/coderai/src/tui/commands.ts';
    export { CapabilityService } from './packages/agent-core-v2/src/app/capability/capabilityService.ts';
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
after(() => rm(dir, { recursive: true, force: true }));

const plain = (component, width = 80) => component.render(width).map(stripTerminalSequences).join('\n');
async function runtimeFixture(t) {
  api.configureTextBuildWorkerRuntime(resolve('dist/text-build-worker.mjs'));
  const f = await fixture(); t.after(f.cleanup);
  const runtime = new api.Runtime({ home: f.home, cwd: f.cwd, trust: true, interactive: true }); t.after(() => runtime.close());
  await runtime.harness.setConfig({ providers: { local: { type: 'openai', baseUrl: f.env.CODERAI_MODEL_BASE_URL, apiKey: 'test' } }, models: { local: { provider: 'local', model: 'fixture-model', maxContextSize: 131072 } }, defaultModel: 'local', watch: { enabled: false }, database: { search: false } });
  await runtime.trust(async () => true); const session = await runtime.open();
  return { f, runtime, session, run: prompt => runtime.run(prompt, () => {}, () => ({ decision: 'approved' })) };
}

test('engine snapshots and SDK diffs survive reload, and multi-prompt undo restores the earliest version of every file', async t => {
  const { f, session, runtime, run } = await runtimeFixture(t);
  assert.equal(await run('SCENARIO phase5-write'), 'completed');
  const first = await session.turnChanges();
  assert.equal(first.recorded, true); assert.deepEqual(first.changes.map(c => [c.path, c.status]), [['created.txt', 'added'], ['input.txt', 'modified']]);
  const diff = await session.turnFileDiff(first.turnId, 'input.txt'); assert.equal(diff.before, 'hello fixture\n'); assert.equal(diff.after, 'changed first\n');
  assert.equal(await run('SCENARIO phase5-edit'), 'completed');
  const second = await session.turnChanges(); assert.ok(second.turnId > first.turnId);
  await session.reloadSession();
  assert.deepEqual(await session.turnChanges(first.turnId), first);
  const notices = [], documents = [];
  await api.command(runtime, { message: text => notices.push(text), showDocument: async (title, body) => documents.push([title, body]) }, `/diff ${first.turnId}`);
  assert.match(documents[0][1], /--- input.txt\n\+\+\+ input.txt\n@@/); assert.match(documents[0][1], /hello fixture/);
  await writeFile(join(f.cwd, 'created.txt'), 'user edit after the turn\n');
  await session.undoHistory(2, { revertFiles: true });
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'hello fixture\n');
  assert.equal(existsSync(join(f.cwd, 'created.txt')), false); assert.equal(existsSync(join(f.cwd, 'later.txt')), false);
  assert.equal((await session.getContext()).history.some(m => JSON.stringify(m.content).includes('SCENARIO phase5-')), false);
  await session.reloadSession(); assert.equal((await session.getContext()).history.some(m => JSON.stringify(m.content).includes('SCENARIO phase5-')), false);
});

test('prompt-only undo preserves files and deleted tracked files can be restored from durable pre-turn bytes', async t => {
  const { f, session, run } = await runtimeFixture(t);
  await run('SCENARIO phase5-delete');
  const changes = await session.turnChanges(); assert.equal(changes.changes[0].status, 'deleted');
  const diff = await session.turnFileDiff(changes.turnId, 'input.txt'); assert.equal(diff.before, 'hello fixture\n'); assert.equal(diff.after, undefined);
  await session.reloadSession(); await session.undoHistory(1, { revertFiles: true });
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'hello fixture\n');
  await run('SCENARIO phase5-write'); await session.undoHistory(1);
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'changed first\n');
});

test('missing and oversize backups reject file rollback before withdrawing prompts or modifying other paths', async t => {
  const { f, session, run } = await runtimeFixture(t);
  await writeFile(join(f.cwd, 'input.txt'), 'large'.repeat(1024 * 1024 + 1));
  await run('SCENARIO phase5-write');
  await assert.rejects(session.undoHistory(1, { revertFiles: true }), /snapshot/);
  assert.equal(await readFile(join(f.cwd, 'created.txt'), 'utf8'), 'first\n');
  assert.ok((await session.getContext()).history.some(m => JSON.stringify(m.content).includes('SCENARIO phase5-write')));
  await session.undoHistory(1);
  await writeFile(join(f.cwd, 'input.txt'), 'hello fixture\n');
  await run('SCENARIO phase5-write');
  await session.turnChanges(); // drain checkpoint writes before inspecting blobs
  const blobs = (await readdir(f.home, { recursive: true })).filter(name => name.includes('file-history') && name.includes('@v') && !name.includes('.tmp.'));
  assert.ok(blobs.length);
  for (const path of blobs) if ((await readFile(join(f.home, path))).equals(Buffer.from('hello fixture\n'))) await rm(join(f.home, path));
  const changes = await session.turnChanges(); assert.equal(changes.recorded, false);
  await assert.rejects(session.undoHistory(1, { revertFiles: true }), /backup is missing/);
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'changed first\n');
  assert.equal(await readFile(join(f.cwd, 'created.txt'), 'utf8'), 'first\n');
});

test('plugin CLI persists enablement, exposes information and MCP toggles, and skills list uses the workspace catalog without a model', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const plugin = join(f.root, 'plugin'); await mkdir(join(plugin, 'skills', 'review'), { recursive: true });
  await writeFile(join(plugin, 'kimi.plugin.json'), JSON.stringify({ name: 'phase5-plugin', version: '1.0.0', description: 'Plugin fixture', skills: ['./skills'], mcpServers: { demo: { command: 'not-started', enabled: false } } }));
  await writeFile(join(plugin, 'skills', 'review', 'SKILL.md'), '---\nname: review\ndescription: Review files\n---\nReview the input.\n');
  let result = await f.run(['plugins', 'install', plugin]); assert.equal(result.code, 0, result.stderr);
  result = await f.run(['plugins', 'info', 'phase5-plugin']); assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /demo/);
  result = await f.run(['plugins', 'disable', 'phase5-plugin']); assert.equal(result.code, 0, result.stderr);
  assert.match((await f.run(['plugins', 'list'])).stdout, /disabled/);
  assert.equal((await f.run(['plugins', 'enable', 'phase5-plugin'])).code, 0);
  assert.equal((await f.run(['plugins', 'mcp', 'enable', 'phase5-plugin', 'demo'])).code, 0);
  assert.equal((await f.run(['plugins', 'mcp', 'disable', 'phase5-plugin', 'demo'])).code, 0);
  result = await f.run(['plugins', 'capabilities', 'list']); assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /kimi-/);
  // Skill discovery remains useful before configuring any provider.
  for (const key of Object.keys(f.env)) if (key.startsWith('CODERAI_MODEL')) delete f.env[key];
  result = await f.run(['--trust', 'skills', 'list']); assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /review/);
  assert.equal((await f.run(['plugins', 'remove', 'phase5-plugin'])).code, 0);
  assert.match((await f.run(['plugins', 'list'])).stdout, /No plugins/);
});

test('engine-backed mentions rank files and directories; inline skills preserve slash, suffix and activation order', async t => {
  const { f, runtime } = await runtimeFixture(t);
  await mkdir(join(f.cwd, 'nested folder')); await writeFile(join(f.cwd, 'nested folder', 'long-name.ts'), '');
  const calls = [];
  const provider = new CompletionProvider([{ name: 'skill:review', skillName: 'review' }, { name: 'mcp' }, { name: 'plugins' }, { name: 'add-dir' }], f.cwd, [], async (root, query) => { calls.push(query); return runtime.harness.suggestFiles(root, { query }); });
  const options = { signal: new AbortController().signal };
  const results = await provider.getSuggestions(['look @lgn'], 0, 9, options);
  assert.ok(results.items.some(item => item.label.includes('long-name.ts'))); assert.deepEqual(calls, ['lgn']);
  const dirs = await provider.getSuggestions(['@nested'], 0, 7, options); assert.ok(dirs.items.some(item => item.label.endsWith('/')));
  const line = 'review /skill:rev and keep this'; const col = 'review /skill:rev'.length;
  const skills = await provider.getSuggestions([line], 0, col, options);
  assert.equal(skills.items[0].value, 'skill:review');
  assert.equal(provider.applyCompletion([line], 0, col, skills.items[0], skills.prefix).lines[0], 'review /skill:review  and keep this');
  const commands = await provider.getSuggestions(['/plugins '], 0, 9, options); assert.ok(commands.items.some(item => item.value === 'capabilities'));
  const map = new Map([['review', 'review'], ['skill:check', 'check']]);
  assert.deepEqual(inlineSkillActivations('Please /skill:review /review /skill:check /tmp/file https://example.test', map), [{ name: 'review' }, { name: 'check' }]);
});

test('Mermaid respects live preferences, completion state, themes, resizing and source fallback', async () => {
  const markdown = new Markdown('Before\n\n```mermaid\nflowchart LR\n A[Alpha] --> B[Beta]\n```\n\nAfter', 1, 0, markdownTheme);
  setMermaidMode('final'); assert.match(plain(markdown), /┌|╭/); assert.match(plain(markdown), /Alpha/); assert.match(plain(markdown), /After/);
  markdown.setTransient(true); assert.match(plain(markdown), /flowchart LR/);
  markdown.setTransient(false); assert.doesNotMatch(plain(markdown), /flowchart LR/);
  setMermaidMode('off'); assert.match(plain(markdown), /flowchart LR/); setMermaidMode('final');
  assert.match(plain(markdown, 12), /mermaid/);
  for (const theme of ['light', 'dark']) {
    await setTheme(theme);
    for (const width of [1, 12, 40, 100]) assert.ok(markdown.render(width).every(line => visibleWidth(line) <= width));
  }
  const invalid = new Markdown('```mermaid\narchitecture-beta\n title Unsupported\n```', 0, 0, markdownTheme);
  assert.match(plain(invalid), /architecture-beta diagrams|could not draw/); assert.match(plain(invalid), /Unsupported/);
});

test('questions retain multi-selection, Other drafts, wrapped bodies and prior answers during back navigation', () => {
  let finished;
  const dialog = new QuestionDialog({ questions: [
    { header: 'First', question: 'Pick first?', body: 'Wrapped body with many words. '.repeat(10), options: [{ label: 'Alpha' }, { label: 'Beta' }], multiSelect: true, otherDescription: 'Describe your custom answer' },
    { question: 'Pick second?', options: [{ label: 'One' }, { label: 'Two' }] },
  ] }, () => 20, value => { finished = value; }, () => {});
  dialog.handleInput(' '); dialog.handleInput('\x1b[B'); dialog.handleInput(' '); // alpha + beta
  dialog.handleInput('\x1b[B'); dialog.handleInput(' '); // Other
  assert.match(plain(dialog), /Describe your custom answer/);
  dialog.handleInput('\x1b[B'); dialog.handleInput('\r'); // Done -> Other
  dialog.handleInput('draft'); dialog.handleInput('\x1b'); // back to choices without losing draft
  dialog.handleInput('\r'); assert.match(plain(dialog), /draft/);
  dialog.handleInput('\r'); // commit -> second
  dialog.handleInput('\x1b'); assert.match(plain(dialog), /\[x\] Alpha/); assert.match(plain(dialog), /\[x\] Beta/);
  dialog.handleInput('\r'); dialog.handleInput('\r'); // Done -> Other -> second
  dialog.handleInput('\x1b[B'); dialog.handleInput('\r'); // Two -> review
  assert.match(plain(dialog), /Alpha, Beta, draft/);
  dialog.handleInput('\r'); assert.equal(finished, 'answered');
  assert.deepEqual(dialog.answers, { 'Pick first?': 'Alpha, Beta, draft', 'Pick second?': 'Two' });
  for (const width of [0, 1, 12, 40]) assert.ok(dialog.render(width).every(line => visibleWidth(line) <= width));
});
async function mcpFixture(f, reply = 'MCP_REPLY_') {
  const server = join(f.root, `mcp-${reply}.cjs`);
  await writeFile(server, `require('node:readline').createInterface({input:process.stdin}).on('line', line => {
    const m = JSON.parse(line); if (m.id === undefined) return;
    const result = m.method === 'initialize' ? {protocolVersion:m.params.protocolVersion, capabilities:{tools:{}}, serverInfo:{name:'fixture',version:'1'}} :
      m.method === 'tools/list' ? {tools:[{name:'echo',description:'Echo',inputSchema:{type:'object',properties:{text:{type:'string'}},required:['text']}}]} :
      m.method === 'tools/call' ? {content:[{type:'text',text:${JSON.stringify(reply)}+m.params.arguments.text}]} : {};
    process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,result})+'\\n');
  });`);
  return { transport: 'stdio', command: process.execPath, args: [server] };
}

test('real MCP: live add, test, reconnect, ephemeral shadowing and reload keep tools and persistence coherent', { timeout: 35000 }, async t => {
  const { f, runtime, session, run } = await runtimeFixture(t);
  const config = await mcpFixture(f);
  const entry = await session.addMcpServer({ name: 'fixture', ...config }, { persist: true });
  assert.equal(entry.status, 'connected'); assert.equal(entry.toolCount, 1);
  assert.equal((await runtime.harness.testMcpServer('fixture', { cwd: f.cwd })).success, true);
  await session.reconnectMcpServer('fixture');
  assert.equal(await run('SCENARIO mcp'), 'completed'); assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('MCP_REPLY_mcp-test-value')));
  const overlayConfig = await mcpFixture(f, 'OVERLAY_');
  const overlay = await runtime.harness.createSession({ workDir: f.cwd, mcpServers: { fixture: overlayConfig } });
  const sibling = await runtime.harness.createSession({ workDir: f.cwd });
  t.after(() => sibling.close()); t.after(() => overlay.close());
  await overlay.getMcpStartupMetrics();
  assert.equal((await overlay.listMcpServers()).filter(s => s.name === 'fixture').length, 1);
  await overlay.reconnectMcpServer('fixture', { name: 'fixture', ...overlayConfig });
  await assert.rejects(overlay.reconnectMcpServer('fixture', { name: 'fixture', ...config, enabled: false }), /disabled/);
  assert.equal((await overlay.addMcpServer({ name: 'ephemeral-extra', ...config })).status, 'connected');
  assert.equal((await sibling.listMcpServers()).some(s => s.name === 'ephemeral-extra'), false);
  await overlay.reloadSession(); await overlay.getMcpStartupMetrics();
  assert.equal((await overlay.listMcpServers()).find(s => s.name === 'ephemeral-extra').status, 'connected');
  assert.equal((await overlay.listMcpServers()).filter(s => s.name === 'fixture').length, 1);
  await overlay.removeMcpServer('ephemeral-extra'); await overlay.reloadSession(); await overlay.getMcpStartupMetrics();
  assert.notEqual((await overlay.listMcpServers()).find(s => s.name === 'ephemeral-extra')?.status, 'connected');
  await session.removeMcpServer('fixture');
  assert.equal(await run('after removing MCP'), 'completed');
  assert.ok(!f.requests.at(-1).tools.some(tool => tool.function.name === 'mcp__fixture__echo'));
  await session.close(); runtime.session = overlay;
  assert.equal(await run('SCENARIO mcp'), 'completed'); assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('OVERLAY_mcp-test-value')));
  const saved = await readFile(join(f.home, 'mcp.json'), 'utf8'); assert.match(saved, /fixture/); assert.doesNotMatch(saved, /ephemeral-extra|OVERLAY/);
});

const terminal = (f, actions, options = {}) => new Promise((done, reject) => {
  const child = spawn('python3', [resolve('test/terminal.py')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let out = '', err = ''; child.stdout.on('data', c => out += c); child.stderr.on('data', c => err += c);
  child.stdin.end(JSON.stringify({ cwd: f.cwd, ...(process.env.CODERAI_TEST_BINARY ? { executable: resolve(process.env.CODERAI_TEST_BINARY) } : { node: testRuntime, cli }), env: { ...f.env, TERM: 'xterm-256color' }, actions, ...options }));
  child.on('error', reject); child.on('close', code => code === 0 ? done(JSON.parse(out)) : reject(new Error(err + out)));
});

test('real PTY: diff and rollback choices restore disk and context through the command path', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: 'SCENARIO phase5-write\r' },
    { wait: 'First file turn complete.', send: '/diff\r' },
    { wait: 'File changes', send: '\x1b' }, { wait: 'Idle', send: '/undo 1\r' },
    { wait: 'Withdraw prompts only', send: '\x1b[B\r' },
    { wait: 'tracked file changes reverted.', send: '\x03' },
    { wait: 'Draft cleared.', send: '/quit\r' },
  ], { args: ['--yolo'] });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'hello fixture\n'); assert.equal(existsSync(join(f.cwd, 'created.txt')), false);
});

test('real PTY: batched question back-navigation preserves prior answers and wrapped body text', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: 'SCENARIO phase6-batch\r' },
    { wait: 'Choose first?', send: '\r' },
    { wait: 'Choose second?', send: '\x1b' },
    { wait: 'Choose first?', send: '\x1b[B\r' },
    { wait: 'Choose second?', send: '\x1b[B\r' },
    { wait: 'Review answers', send: '\r' },
    { wait: 'Batch answers received.', send: '/quit\r' },
  ], { columns: 52, rows: 20 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.match(result.output, /A long question body/);
  assert.ok(f.requests.some(request => JSON.stringify(request.messages).includes('Beta') && JSON.stringify(request.messages).includes('Two')));
});

test('real PTY: fullscreen search, wheel scrolling, links, resize and cancellation restore the terminal', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.home, 'tui.toml'), 'tui_mode = "fullscreen"\ntheme = "light"\nauto_session_title = false\n');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: 'SCENARIO phase6-mermaid\r' },
    { wait: 'Transcript scroll marker', send: '\x1b[5~' },
    { wait: 'Jump to bottom', send: '/' },
    { wait: 'Find in transcript', send: 'Alpha' },
    { wait: '1/1', send: '\r' },
    { wait: '', send: '\x1b' },
    { wait: '', resize: { columns: 52, rows: 18 }, send: '\x1b[<64;10;3M\x1b[<65;10;3M' },
    { wait: '', send: '\x1b[F' },
    { wait: '', send: '/new\r' },
    { wait: 'New session', send: '/quit\r' },
  ], { columns: 90, rows: 26 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  const raw = Buffer.from(result.raw, 'base64').toString();
  assert.match(raw, /\x1b\[\?1049h/); assert.match(raw, /\x1b\[\?1049l/);
  assert.match(raw, /\x1b\[\?1006h/); assert.match(raw, /\x1b\[\?1006l/);
  assert.match(raw, /https:\/\/example.test\/coderai/);
  assert.doesNotMatch(result.output, /This version uses the regular layout/);
});

test('binary snapshots restore exact bytes; expired turns require prompt-only undo', async t => {
  const { f, session, run } = await runtimeFixture(t);
  const bytes = Buffer.from([0, 255, 254, 12, 13]); await writeFile(join(f.cwd, 'input.txt'), bytes);
  await run('SCENARIO phase5-write');
  const turn = await session.turnChanges(); assert.equal(turn.changes.find(c => c.path === 'input.txt').binary, true);
  await session.undoHistory(1, { revertFiles: true }); assert.deepEqual(await readFile(join(f.cwd, 'input.txt')), bytes);
  for (let i = 0; i < 6; i++) { await writeFile(join(f.cwd, 'input.txt'), `before ${i}\n`); await run('SCENARIO phase5-write'); }
  await assert.rejects(session.undoHistory(6, { revertFiles: true }), /expired/);
  assert.equal(await readFile(join(f.cwd, 'input.txt'), 'utf8'), 'changed first\n');
  await session.undoHistory(6); await assert.rejects(session.turnChanges(), /No such turn/);
});

test('fullscreen host search entry preserves focus and links dispatch through the terminal mouse path', () => {
  const writes = [], opened = []; let input;
  const terminal = { columns: 60, rows: 12, kittyProtocolActive: false,
    start: onInput => { input = onInput; }, stop() {}, drainInput: async () => {}, write: text => writes.push(text),
    hideCursor() {}, showCursor() {}, clearLine() {}, clearFromCursor() {}, clearScreen() {}, moveBy() {}, setTitle() {}, setProgress() {},
  };
  const tui = new TuiAltScreen(terminal, false, undefined, { openUrl: url => opened.push(url) });
  const document = new Text('\x1b]8;;https://example.test/link\x07Clickable link\x1b]8;;\x07', 0, 0);
  const focus = { focused: false, render: () => ['draft preserved'], invalidate() {}, handleInput() {} };
  const root = new VStack([{ component: new ScrollView(document, { primary: true, follow: 'end' }), basis: 0, grow: 1 }, focus]);
  tui.setLayoutRoot(root); tui.setFocus(focus); tui.start();
  try {
    tui.renderNow(true);
    input('\x1b[<0;3;1M'); input('\x1b[<0;3;1m');
    assert.deepEqual(opened, ['https://example.test/link']);
    tui.openTranscriptSearch(); assert.equal(tui.hasOverlay(), true); assert.equal(focus.focused, false);
    input('Clickable'); tui.renderNow();
    assert.match(writes.join(''), /1\/1/);
    input('\x1b'); assert.equal(tui.hasOverlay(), false); assert.equal(focus.focused, true);
    assert.equal(focus.render()[0], 'draft preserved');
  } finally { tui.stop(); }
});

test('capability installs follow the engine background state machine without duplicate starts and report failures', async () => {
  let installed = false, starts = 0, fail = false;
  const service = new api.CapabilityService({}, {}, {}, { warn() {} }, {}, [{
    id: 'kimi-cu', displayName: 'Test capability', description: 'Controlled installer', supported: true,
    detect: async () => ({ steps: [{ id: 'runtime', state: installed ? 'ok' : 'missing' }] }),
    install: async report => { starts++; report('runtime', 50); await new Promise(resolve => setTimeout(resolve, 20)); if (fail) throw new Error('Installer failed for test'); installed = true; },
  }]);
  const progress = [];
  const initial = await service.installCapability('kimi-cu'); assert.equal(initial.install.running, true);
  const result = await installCapabilityAndWait(service, 'kimi-cu', { onProgress: status => progress.push(status.install.step) });
  assert.equal(result.state, 'ready'); assert.equal(starts, 1); assert.ok(progress.includes('runtime'));
  fail = true;
  await assert.rejects(installCapabilityAndWait(service, 'kimi-cu'), /Installer failed/);
  assert.match(capabilityText(await service.getCapability('kimi-cu')), /Install failed: Installer failed/);
  const abort = new AbortController(); abort.abort();
  await assert.rejects(installCapabilityAndWait(service, 'kimi-cu', { signal: abort.signal }), { name: 'AbortError' });
  assert.equal(starts, 2); service.dispose();
});

test('real PTY: plugin panel handles an empty install set, fuzzy actions and cancellation', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: '/plugins\r' },
    { wait: 'No plugins installed.', send: 'cpb\r' },
    { wait: 'Capability readiness', send: '\x1b' },
    { wait: 'Plugins', send: '\x1b' },
    { wait: 'Idle', send: '/quit\r' },
  ]);
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
});

test('real PTY: fullscreen keeps /btw responsive during main work and resumes streaming after a session switch', { timeout: 40000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await writeFile(join(f.home, 'tui.toml'), 'tui_mode = "fullscreen"\nauto_session_title = false\n[notifications]\nenabled = true\nnotification_condition = "always"\n');
  const result = await terminal(f, [
    { wait: 'Trust ', send: 'yes\r' }, { wait: 'Idle', send: 'SCENARIO wait\r' },
    { wait: 'Working', send: '/btw side question\r' },
    { wait: 'Hello from the deterministic provider.', send: '/btw close\r' },
    { wait: 'Side question closed.', send: '\x03' },
    { wait: 'Turn cancelled', send: '/new\r' },
    { wait: 'New session', send: 'SCENARIO terminal-escape\r' },
    { wait: 'Safe  text', send: '/quit\r' },
  ], { columns: 65, rows: 22 });
  assert.equal(result.code, 0, result.output); assert.ok(result.terminalRestored);
  assert.doesNotMatch(Buffer.from(result.raw, 'base64').toString(), /TERMINAL_INJECTION/);
  assert.ok(f.requests.length >= 3);
});
