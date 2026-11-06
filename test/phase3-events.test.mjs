import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
const { eventNotice, McpAuthorizationOpener, parseMcpAuthorization } = await import('../apps/coderai/src/event-format.ts');
const { SubagentActivityStore, agentActivityText, ACTIVITY_STEPS, ACTIVITY_TEXT_LIMIT, ACTIVITY_OUTPUT_LIMIT } = await import('../apps/coderai/src/tui/subagent-activity.ts');
const { ConversationTranscript } = await import('../apps/coderai/src/tui/transcript.ts');
const { BtwPanelController } = await import('../apps/coderai/src/tui/btw-panel.ts');
const { AutoSessionTitle } = await import('../apps/coderai/src/tui/session-title.ts');
const { loadTuiConfig, saveAutoSessionTitle } = await import('../apps/coderai/src/tui/config.ts');
const { headless } = await import('../apps/coderai/src/headless.ts');
const { stripTerminalSequences, visibleWidth } = await import('../packages/pi-tui/src/utils.ts');
const plain = component => component.render(100).map(stripTerminalSequences).join('\n');
const event = (type, data = {}) => ({ type, sessionId: 'session', agentId: 'main', ...data });
const spawn = (id, data = {}) => event('subagent.spawned', { subagentId: id, subagentName: `worker-${id}`, parentToolCallId: 'parent', runInBackground: false, ...data });

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

test('shared notices retain hook blocking reasons, cron metadata, interruption reasons and real plugin event type', () => {
  assert.equal(eventNotice(event('hook.result', { hookEvent: 'PreToolUse', content: ' blocked because policy ', blocked: true })), 'PreToolUse hook blocked\n\nblocked because policy');
  assert.equal(eventNotice(event('hook.result', { hookEvent: 'Stop', content: '  ' })), 'Stop hook\n\n(empty)');
  const cron = eventNotice(event('cron.fired', { prompt: 'Run checks', origin: { jobId: 'job-1', cron: '* * * * *', recurring: false, coalescedCount: 3, stale: true } }));
  for (const text of ['Run checks', 'job job-1', '* * * * *', 'one-shot', '3 fires coalesced', 'final delivery']) assert.ok(cron.includes(text));
  assert.equal(eventNotice(event('turn.step.interrupted', { reason: 'aborted' })), 'Interrupted by user');
  assert.equal(eventNotice(event('turn.step.interrupted', { reason: 'max_steps' })), 'reached per-turn step limit (max_steps)');
  assert.equal(eventNotice(event('turn.step.interrupted', { reason: 'error' })), undefined);
  assert.equal(eventNotice(event('turn.step.interrupted', { reason: 'aborted', message: 'Cancelled by command' })), 'Cancelled by command');
  assert.match(eventNotice(event('plugin_command.activated', { pluginId: 'local', commandName: 'review', commandArgs: 'file' })), /\/local:review file/);
  assert.match(eventNotice(event('tool.list.updated', { serverName: 'local', reason: 'mcp.connected' })), /local.*mcp.connected/);
});

test('OAuth progress opens a validated URL once per agent/call/URL and leaves a fallback after browser failure', async () => {
  const messages = [], urls = [];
  const opener = new McpAuthorizationOpener(text => messages.push(text), async url => { urls.push(url); return false; });
  const update = { kind: 'custom', customKind: 'mcp.oauth.authorization_url', customData: { serverName: 'example', authorizationUrl: 'https://example.test/authorize?secret=abc' } };
  const progress = event('tool.progress', { toolCallId: 'call', update });
  opener.handle(progress); opener.handle(progress);
  await Promise.resolve();
  assert.equal(urls.length, 1); assert.match(messages.join('\n'), /https:\/\/example.test\/authorize/); assert.match(messages.at(-1), /could not open/);
  opener.handle({ ...progress, agentId: 'child' }); assert.equal(urls.length, 2);
  for (const invalid of ['file:///etc/passwd', 'javascript:alert(1)', 'https://user:secret@example.test/', 'https://example.test/\n']) {
    assert.equal(parseMcpAuthorization({ ...update, customData: { serverName: 'example', authorizationUrl: invalid } }), undefined);
  }
  for (const invalid of [null, [], {}, { serverName: '', authorizationUrl: 'https://example.test' }]) assert.equal(parseMcpAuthorization({ ...update, customData: invalid }), undefined);
  opener.clear(); opener.handle(progress); assert.equal(urls.length, 3);
});

test('streamed tool arguments precede authoritative starts without duplicate cards or cross-agent collisions', () => {
  const transcript = new ConversationTranscript();
  transcript.toolDelta({ agentId: 'main', toolCallId: 'call', name: 'Bash', argumentsPart: '{"command":"par' });
  transcript.toolDelta({ agentId: 'main', toolCallId: 'call', argumentsPart: 'tial"}' });
  assert.match(plain(transcript), /partial/);
  transcript.toolStarted({ agentId: 'main', toolCallId: 'call', name: 'Bash', args: { command: 'authoritative' } });
  transcript.setExpanded(true);
  assert.match(plain(transcript), /authoritative/); assert.doesNotMatch(plain(transcript), /partial/);
  assert.equal((plain(transcript).match(/Tool: Bash/g) ?? []).length, 1);
  transcript.toolStarted({ agentId: 'child', toolCallId: 'call', name: 'Read', args: { path: 'child.txt' } });
  transcript.toolResult({ agentId: 'child', toolCallId: 'call', output: 'child result' });
  transcript.toolResult({ agentId: 'main', toolCallId: 'call', output: 'parent result' });
  assert.match(plain(transcript), /child result/); assert.match(plain(transcript), /parent result/);
  transcript.toolDelta({ agentId: 'main', toolCallId: 'todo', argumentsPart: '{"todos":' });
  transcript.hideTool('main', 'todo'); transcript.finishTurn();
  assert.doesNotMatch(plain(transcript), /todos|Read 0 files/);
  for (const width of [0, 1, 10, 50]) assert.ok(transcript.render(width).every(line => visibleWidth(line) <= width));
});

test('interleaved AgentSwarm children stream into the parent card and remain independently inspectable', () => {
  const transcript = new ConversationTranscript(), store = new SubagentActivityStore();
  transcript.toolStarted({ agentId: 'main', toolCallId: 'parent', name: 'AgentSwarm' });
  const apply = e => { const record = store.apply(e); if (record) transcript.agentActivity(record); };
  apply(spawn('one', { swarmIndex: 1 })); apply(spawn('two', { swarmIndex: 2 }));
  apply(event('assistant.delta', { agentId: 'one', delta: 'one answer' }));
  apply(event('thinking.delta', { agentId: 'two', delta: 'two thinking' }));
  apply(event('tool.call.delta', { agentId: 'one', toolCallId: 'same', name: 'Read', argumentsPart: '{"path":"first"}' }));
  apply(event('tool.call.started', { agentId: 'one', toolCallId: 'same', name: 'Read', args: { path: 'one.txt' } }));
  apply(event('tool.call.started', { agentId: 'two', toolCallId: 'same', name: 'Bash', args: { command: 'second' } }));
  apply(event('tool.progress', { agentId: 'two', toolCallId: 'same', update: { kind: 'stdout', text: 'old' } }));
  apply(event('tool.progress', { agentId: 'two', toolCallId: 'same', update: { kind: 'stdout', text: 'new', replace: true } }));
  apply(event('agent.status.updated', { agentId: 'one', model: 'local-model', thinkingEffort: 'high', contextTokens: 777 }));
  apply(event('tool.result', { agentId: 'one', toolCallId: 'same', output: 'one result' }));
  apply(event('subagent.completed', { subagentId: 'one', resultSummary: 'one done' }));
  assert.match(plain(transcript), /Agents: 1\/2 completed/); assert.match(plain(transcript), /worker-one/); assert.match(plain(transcript), /worker-two/);
  assert.equal(store.get('two').steps[0].tools[0].progress, 'new');
  transcript.setExpanded(true);
  for (const text of ['one answer', 'two thinking', 'one.txt', 'one result', 'local-model', '777 context tokens']) assert.ok(plain(transcript).includes(text), text);
  assert.equal(transcript.lastAssistantText, '');
  assert.doesNotMatch(agentActivityText(store.get('one')), /two thinking|second/);
  apply(event('background.task.started', { info: { kind: 'agent', agentId: 'two', description: 'second', taskId: 'bg', status: 'running' } }));
  transcript.toolResult({ agentId: 'main', toolCallId: 'parent', output: 'Spawned successfully' });
  apply(event('background.task.terminated', { info: { kind: 'agent', agentId: 'two', taskId: 'bg', status: 'timed_out' } }));
  assert.match(plain(transcript), /Tool failed: AgentSwarm/);
  apply(event('subagent.completed', { subagentId: 'two', resultSummary: 'late result' }));
  assert.equal(store.get('two').status, 'timed_out');
  apply(spawn('two', { runInBackground: true })); assert.equal(store.get('two').status, 'running'); assert.equal(store.get('two').summary, undefined);
  store.finishForeground(); assert.equal(store.get('two').status, 'running');
});

test('activity captures agents without spawn events, bounds retained steps/arguments/text/results, and unsubscribes', () => {
  const store = new SubagentActivityStore(); let updates = 0;
  const off = store.subscribe(() => updates++);
  for (let step = 1; step <= ACTIVITY_STEPS + 5; step++) {
    store.apply(event('turn.step.started', { agentId: 'unknown', step }));
    store.apply(event('assistant.delta', { agentId: 'unknown', delta: 'x'.repeat(ACTIVITY_TEXT_LIMIT * 2) }));
    store.apply(event('tool.call.delta', { agentId: 'unknown', toolCallId: String(step), argumentsPart: 'a'.repeat(ACTIVITY_TEXT_LIMIT * 2) }));
    store.apply(event('tool.result', { agentId: 'unknown', toolCallId: String(step), output: 'o'.repeat(ACTIVITY_OUTPUT_LIMIT * 2) }));
  }
  const record = store.get('unknown');
  assert.equal(record.totalSteps, ACTIVITY_STEPS + 5); assert.equal(record.steps.length, ACTIVITY_STEPS);
  assert.equal(record.steps[0].step, 6);
  assert.ok(record.steps.every(step => step.text.length <= ACTIVITY_TEXT_LIMIT && step.tools.every(call => call.args.length <= ACTIVITY_TEXT_LIMIT && call.output.length <= ACTIVITY_OUTPUT_LIMIT)));
  const before = updates; off(); store.clear(); assert.equal(updates, before); assert.deepEqual(store.all(), []);
});

function btwFixture() {
  const listeners = new Set(), calls = [], cancellations = [], errors = []; let target = 'main', next = 0, panel;
  const session = { id: 'current-session', isClosed: false, async startBtw() { return `btw-${++next}`; },
    onEvent(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    async prompt(text) { calls.push({ agentId: target, text }); }, async cancel() { cancellations.push(target); },
  };
  const harness = { withInteractiveAgent(id, fn) { const previous = target; target = id; try { return fn(); } finally { target = previous; } } };
  const controller = new BtwPanelController(harness, value => panel = value, () => {}, () => 16, error => errors.push(error));
  const emit = e => { for (const fn of listeners) fn(e); };
  return { controller, session, calls, cancellations, errors, emit, listeners, get panel() { return panel; } };
}

test('/btw uses the active session, isolates main output, supports follow-ups and targets cancellation to its child', async () => {
  const f = btwFixture();
  await f.controller.open(f.session, 'side question');
  assert.deepEqual(f.calls, [{ agentId: 'btw-1', text: 'side question' }]);
  assert.equal(f.controller.send('too early'), false);
  f.emit(event('assistant.delta', { delta: 'MAIN SECRET' }));
  f.emit(event('assistant.delta', { agentId: 'btw-1', delta: 'side answer' }));
  f.emit(event('turn.ended', { agentId: 'main', reason: 'completed' }));
  assert.equal(f.controller.isRunning, true); assert.doesNotMatch(plain(f.panel), /MAIN SECRET/);
  f.emit(event('turn.ended', { agentId: 'btw-1', reason: 'completed' }));
  assert.equal(f.controller.send('follow-up'), true); assert.equal(f.calls.length, 2);
  assert.equal(f.controller.cancel(), true); assert.deepEqual(f.cancellations, ['btw-1']);
  f.emit(event('turn.ended', { agentId: 'btw-1', reason: 'cancelled' }));
  assert.equal(f.controller.close(), true); assert.equal(f.panel, undefined); assert.equal(f.listeners.size, 0);
  assert.deepEqual(f.errors, []); await f.controller.dispose();
});

test('/btw drops late startup after close and failed dispatch leaves a usable panel', async () => {
  const f = btwFixture(), pending = deferred();
  f.session.startBtw = () => pending.promise;
  const opening = f.controller.open(f.session, 'never sent');
  f.controller.close(); pending.resolve('late-agent'); await opening;
  assert.equal(f.panel, undefined); assert.equal(f.calls.length, 0);
  f.session.startBtw = async () => 'new-agent'; f.session.prompt = async () => { throw new Error('provider unavailable'); };
  await f.controller.open(f.session, 'try'); await Promise.resolve();
  assert.equal(f.controller.isRunning, false); assert.match(plain(f.panel), /provider unavailable/);
  for (const width of [1, 10, 50]) assert.ok(f.panel.render(width).every(line => visibleWidth(line) <= width));
  await f.controller.dispose();
});

test('automatic titles coalesce concurrent completions, stop after success, honor both preferences and cap unavailable retries', async () => {
  const pending = deferred(), calls = [], updated = []; let enabled = true, configEnabled = true;
  const harness = { async getConfig() { return { autoSessionTitle: configEnabled }; }, async generateSessionTitle(input) { calls.push(input); return pending.promise; } };
  const titles = new AutoSessionTitle(harness, () => enabled, (...args) => updated.push(args));
  const first = titles.completed('s'); assert.equal(titles.completed('s'), first); await Promise.resolve();
  pending.resolve('Generated title'); await first; await titles.completed('s');
  assert.deepEqual(calls, [{ id: 's', source: 'first_turn' }]); assert.deepEqual(updated, [['s', 'Generated title']]);
  enabled = false; await titles.completed('disabled'); assert.equal(calls.length, 1);
  enabled = true; configEnabled = false; await titles.completed('config-disabled'); assert.equal(calls.length, 1);
  let unavailable = 0;
  const noTitle = new AutoSessionTitle({ async getConfig() { return {}; }, async generateSessionTitle() { unavailable++; if (unavailable === 1) throw new Error('network'); } }, () => true, () => assert.fail('No title should be applied'));
  for (let i = 0; i < 10; i++) await noTitle.completed('untitled');
  assert.equal(unavailable, 3);
});

test('automatic title preference persists locally and preserves other TOML settings', async t => {
  const home = await mkdtemp(join(tmpdir(), 'coderai-phase3-')); t.after(() => rm(home, { recursive: true, force: true }));
  assert.equal((await loadTuiConfig(home)).autoSessionTitle, true);
  await writeFile(join(home, 'tui.toml'), 'theme = "light"\n[editor]\ncommand = "vi"\n');
  await saveAutoSessionTitle(home, false); assert.equal((await loadTuiConfig(home)).autoSessionTitle, false);
  const text = await readFile(join(home, 'tui.toml'), 'utf8'); assert.match(text, /theme = "light"/); assert.match(text, /command = "vi"/);
  await writeFile(join(home, 'tui.toml'), 'auto_session_title = "false"');
  await assert.rejects(loadTuiConfig(home), /boolean/);
});

test('headless plain diagnostics handle ignored events and stale warnings while JSON preserves raw events', async () => {
  const events = [event('hook.result', { hookEvent: 'PreToolUse', content: 'policy reason', blocked: true }),
    event('cron.fired', { prompt: 'scheduled work', origin: { jobId: 'job', cron: '* * * * *', recurring: true } }),
    event('turn.step.interrupted', { reason: 'max_steps' }), event('session.meta.updated', { patch: { title: 'new title' } }),
    event('plugin_command.activated', { pluginId: 'example', commandName: 'review' }),
    event('tool.call.delta', { toolCallId: 'call', name: 'Read', argumentsPart: '{"path":' }),
    event('tool.list.updated', { serverName: 'server', reason: 'mcp.connected' }), event('assistant.delta', { delta: 'answer' })];
  let handler; const session = { id: 's', workDir: '/test', getSessionWarnings: async () => [{ message: 'session warning', severity: 'warning' }], setStaleReloadHandler(fn) { handler = fn; } };
  const runtime = { session, options: {}, open: async () => session, async run(_prompt, emit) { await handler(); for (const e of events) emit(e); return 'completed'; } };
  const originalOut = process.stdout.write, originalErr = process.stderr.write; let out = '', err = '';
  try {
    process.stdout.write = chunk => { out += chunk; return true; }; process.stderr.write = chunk => { err += chunk; return true; };
    assert.equal(await headless(runtime, 'test', false), 0); assert.equal(out, 'answer\n'); assert.equal(handler, undefined);
    for (const text of ['hook blocked', 'policy reason', 'scheduled work', 'job job', 'max_steps', 'new title', '/example:review', 'Read', 'Tools updated', 'session warning', 'reloaded']) assert.ok(err.includes(text), text);
    out = ''; err = ''; assert.equal(await headless(runtime, 'test', true), 0);
    const json = out.trim().split('\n').map(line => JSON.parse(line));
    for (const e of events) assert.ok(json.some(value => value.type === e.type));
    assert.ok(json.some(value => value.type === 'coderai.session.reloaded'));
  } finally { process.stdout.write = originalOut; process.stderr.write = originalErr; }
});

test('agent activity inspection follows store updates and releases its subscription on close', async () => {
  const { TerminalView } = await import('../apps/coderai/src/tui/view.ts');
  const view = new TerminalView('/tmp/coderai-phase3-view', '/tmp', []);
  view.agents.apply(spawn('viewer'));
  let viewer;
  view.showDialog = async create => {
    viewer = create(() => {});
    assert.match(plain(viewer), /worker-viewer/);
    view.agents.apply(event('assistant.delta', { agentId: 'viewer', delta: 'live inspection text' }));
    assert.match(plain(viewer), /live inspection text/);
    view.agents.apply(event('subagent.failed', { subagentId: 'viewer', error: 'failure diagnosis' }));
    assert.match(plain(viewer), /failure diagnosis/);
    return '';
  };
  await view.showAgentActivity('viewer');
  const after = plain(viewer);
  view.agents.apply(spawn('viewer')); view.agents.apply(event('assistant.delta', { agentId: 'viewer', delta: 'after close' }));
  assert.equal(plain(viewer), after);
  view.stop();
});

test('swarm progress includes queued and resumed members and parses current and legacy terminal outcomes', async () => {
  const { AgentSwarmProgressComponent, swarmOutcomes } = await import('../apps/coderai/src/tui/agent-swarm-progress.ts');
  const store = new SubagentActivityStore();
  const first = store.apply(spawn('resumed', { swarmIndex: 1 }));
  const progress = new AgentSwarmProgressComponent(new Map([[first.agentId, first]]));
  progress.setArgs({ resume_agent_ids: { resumed: 'continue' }, items: ['item A', 'item B'] });
  assert.match(plain(progress), /Agents: 0\/3 completed/); assert.match(plain(progress), /item A · queued/);
  store.apply(event('subagent.suspended', { subagentId: 'resumed', reason: 'rate limit' }));
  assert.match(plain(progress), /suspended/);
  const output = '<subagent index="1" outcome="completed">done</subagent>\n<subagent index="2" outcome="failed">failed reason</subagent>\n<subagent index="3" outcome="aborted">cancelled</subagent>';
  progress.complete(output, false);
  assert.match(plain(progress), /Agents: 1\/3 completed/); assert.match(plain(progress), /item A · failed/); assert.match(plain(progress), /item B · cancelled/);
  assert.deepEqual(swarmOutcomes('[agent 1]\nstatus: completed\n\n[agent 2]\nstatus: aborted').map(value => [value.index, value.status]), [[1, 'completed'], [2, 'cancelled']]);
  assert.deepEqual(swarmOutcomes('<subagent index="999999" outcome="completed">bad</subagent>'), []);
  for (const width of [1, 10, 50]) assert.ok(progress.render(width).every(line => visibleWidth(line) <= width));
});

test('child activity cannot inject terminal controls into the parent card', () => {
  const transcript = new ConversationTranscript(), store = new SubagentActivityStore();
  transcript.toolStarted({ agentId: 'main', toolCallId: 'parent', name: 'Agent' });
  transcript.agentActivity(store.apply(spawn('safe')));
  store.apply(event('assistant.delta', { agentId: 'safe', delta: '\x1b]52;c;secret' }));
  store.apply(event('assistant.delta', { agentId: 'safe', delta: '\x07safe answer' }));
  const rendered = transcript.render(100).join('\n');
  assert.doesNotMatch(rendered, /52;c;secret/); assert.match(rendered, /safe answer/);
});

test('swarm final failures and cancellations are visible when queued members never spawn', () => {
  const transcript = new ConversationTranscript();
  transcript.toolStarted({ agentId: 'main', toolCallId: 'failed-swarm', name: 'AgentSwarm', args: { items: ['not started'] } });
  transcript.toolResult({ agentId: 'main', toolCallId: 'failed-swarm', output: '<subagent index="1" outcome="failed">no capacity</subagent>' });
  assert.match(plain(transcript), /Tool failed: AgentSwarm/);
  transcript.toolStarted({ agentId: 'main', toolCallId: 'cancelled-swarm', name: 'AgentSwarm', args: { items: ['cancelled'] } });
  transcript.toolResult({ agentId: 'main', toolCallId: 'cancelled-swarm', output: '<subagent index="1" outcome="aborted">user cancelled</subagent>' });
  assert.match(plain(transcript), /Tool interrupted: AgentSwarm/);
});

test('hook output keeps its transcript position between main response deltas', async () => {
  const { renderEventNotice } = await import('../apps/coderai/src/event-format.ts');
  const { TerminalView } = await import('../apps/coderai/src/tui/view.ts');
  const view = new TerminalView('/tmp/coderai-phase3-order', '/tmp', []);
  view.tui.requestRender = () => {};
  view.delta('before-hook');
  renderEventNotice(view, event('hook.result', { hookEvent: 'Stop', content: 'hook-content' }));
  view.delta('after-hook');
  const text = plain(view.transcript);
  assert.ok(text.indexOf('before-hook') < text.indexOf('hook-content'));
  assert.ok(text.indexOf('hook-content') < text.indexOf('after-hook'));
  assert.equal(view.transcript.lastAssistantText, 'before-hookafter-hook');
});
