import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const { describeToolInput } = await import('../apps/coderai/src/tui/display.ts');
const { CompletionProvider } = await import('../apps/coderai/src/tui/completion.ts');
const { ActivityIndicator, StatusFooter } = await import('../apps/coderai/src/tui/status.ts');
const { TurnActivity } = await import('../apps/coderai/src/tui/activity.ts');
const { TodoListComponent, selectVisibleTodos, todoListFromResult } = await import('../apps/coderai/src/tui/todos.ts');
const { recoveryNotice } = await import('../apps/coderai/src/tui/recovery.ts');
const { stripTerminalSequences, visibleWidth } = await import('../packages/pi-tui/src/utils.ts');
const plain = component => component.map(stripTerminalSequences).join('\n');

test('footer stays at two lines, keeps important modes, and hides empty counters', () => {
  const footer = new StatusFooter();
  const status = { model: '__kimi_env_model__', permission: 'yolo', planMode: true, thinkingEffort: 'high', contextTokens: 131072, maxContextTokens: 262144 };
  footer.setState(status, 'project', 'Example Model');
  for (const width of [20, 38, 50, 80, 110]) {
    const lines = footer.render(width);
    assert.equal(lines.length, 2);
    assert.ok(lines.every(line => visibleWidth(line) <= width));
    assert.match(plain(lines), /\[PLAN\].*\[YOLO\]/);
    assert.match(plain(lines), /Context 50%/);
    assert.doesNotMatch(plain(lines), /queued|attachments|__kimi_env_model__|131072/);
  }
  assert.match(plain(footer.render(80)), /Example Model.*project/);
  footer.setState({ ...status, permission: 'auto', planMode: false, maxContextTokens: 0 }, 'project', 'Example Model', 1);
  assert.match(plain(footer.render(80)), /\[AUTO\]/);
  assert.match(plain(footer.render(80)), /Context unavailable · 1 attachment/);
  assert.doesNotMatch(plain(footer.render(80)), /NaN|Infinity/);
});

test('activity shows elapsed time, retry countdown and request phase, then clears on idle', () => {
  let now = 1000;
  const activity = new ActivityIndicator(() => {}, () => now);
  activity.setActivity('Working · Waiting for model');
  now += 2500;
  assert.match(plain(activity.render(80)), /Waiting for model · 2s · Esc cancel/);
  activity.setRetry({ nextAttempt: 2, maxAttempts: 4, delayMs: 3000, errorName: 'RateLimit', errorMessage: 'Slow down\ntry later', statusCode: 429 });
  assert.match(plain(activity.render(80)), /Retrying \(2\/4\) · in 3s/);
  assert.match(plain(activity.render(80)), /429 · Slow down try later/);
  now += 3100;
  assert.match(plain(activity.render(80)), /requesting model/);
  assert.doesNotMatch(plain(activity.render(80)), /in 0s/);
  for (const width of [3, 12, 38, 50]) assert.ok(activity.render(width).every(line => visibleWidth(line) <= width));
  activity.setActivity('Writing response');
  assert.doesNotMatch(plain(activity.render(80)), /429|Retrying/);
  activity.setText('Idle');
  assert.equal(plain(activity.render(80)).trim(), 'Idle');
});

test('activity animation stops on idle, suspension and disposal', async () => {
  let renders = 0;
  const activity = new ActivityIndicator(() => renders++);
  activity.setActivity('Thinking'); activity.start();
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.ok(renders > 0);
  activity.stop(); const stopped = renders;
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(renders, stopped);
  activity.start(); activity.setText('Idle');
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(renders, stopped);
});

test('activity follows foreground tools and ignores subagents and open dialogs', () => {
  const calls = [];
  const view = { dialogOpen: false, setActivity: (...args) => calls.push(args), setRetry: retry => calls.push(['retry', retry.nextAttempt]) };
  const activity = new TurnActivity(view);
  const main = { agentId: 'main', turnId: 1 };
  activity.handle({ ...main, type: 'tool.call.started', toolCallId: 'read', name: 'Read', description: 'Reading source', display: { kind: 'file_io', path: 'source.ts' } });
  activity.handle({ ...main, type: 'tool.call.started', toolCallId: 'test', name: 'Bash', description: 'Running tests', display: { kind: 'command', command: 'bun test' } });
  activity.handle({ ...main, type: 'tool.result', toolCallId: 'read' });
  assert.deepEqual(calls.at(-1), ['Running tests', '$ bun test']);
  activity.handle({ ...main, type: 'tool.progress', toolCallId: 'test', update: { text: '3 tests passed' } });
  assert.deepEqual(calls.at(-1), ['Running tests', '3 tests passed']);
  const count = calls.length;
  activity.handle({ ...main, agentId: 'worker', type: 'thinking.delta', delta: 'thought' });
  view.dialogOpen = true;
  activity.handle({ ...main, type: 'turn.step.retrying', nextAttempt: 2 });
  assert.equal(calls.length, count);
  view.dialogOpen = false;
  activity.handle({ ...main, type: 'tool.result', toolCallId: 'test' });
  assert.deepEqual(calls.at(-1), ['Working · Waiting for model', '']);
  activity.handle({ ...main, type: 'assistant.delta', delta: 'done' });
  assert.deepEqual(calls.at(-1), ['Writing response', '']);
});

test('todo cards prioritize active work, retain snapshots, and expand every item', () => {
  const todos = [
    { title: 'Finished earlier', status: 'done' },
    { title: 'Latest finished', status: 'done' },
    { title: 'Next task', status: 'pending' },
    { title: 'Active task', status: 'in_progress' },
    { title: 'Later task', status: 'pending' },
  ];
  assert.deepEqual(selectVisibleTodos(todos).map(item => item.title), ['Latest finished', 'Active task']);
  const card = new TodoListComponent(); card.setItems(todos);
  todos[3].title = 'Mutated later';
  assert.match(plain(card.render(80)), /Todos · 2\/5 done · 1 active/);
  assert.match(plain(card.render(80)), /✓ Latest finished/);
  assert.match(plain(card.render(80)), /● Active task/);
  assert.match(plain(card.render(80)), /\+3 more .*Ctrl\+O expand/);
  card.expanded = true;
  assert.match(plain(card.render(80)), /○ Next task/);
  assert.doesNotMatch(plain(card.render(80)), /more|Mutated later/);
  for (const width of [1, 3, 20, 38, 80]) assert.ok(card.render(width).every(line => visibleWidth(line) <= width));
  const pinned = new TodoListComponent(true);
  pinned.setItems([{ title: 'Done', status: 'done' }]);
  assert.equal(pinned.render(80).length, 0);
  pinned.setItems([]); assert.deepEqual(pinned.render(80), []);
  card.setItems([]); assert.match(plain(card.render(80)), /No todos\./);
});

test('todo result parsing distinguishes successful clears from malformed output', () => {
  assert.deepEqual(todoListFromResult('Todo list updated.\nCurrent todo list:\n  [done] First\n  [in_progress] Second\n\nNext step hint'), [
    { status: 'done', title: 'First' }, { status: 'in_progress', title: 'Second' },
  ]);
  assert.deepEqual(todoListFromResult('Todo list cleared.'), []);
  assert.deepEqual(todoListFromResult('Todo list is empty.'), []);
  assert.equal(todoListFromResult('Current todo list:\n  [pending] Multi\nline'), undefined);
  assert.equal(todoListFromResult('Tool failed'), undefined);
});

test('errors offer specific recovery and treat cancellation separately', () => {
  assert.match(recoveryNotice({ code: 'provider.auth_error', message: 'Bad credentials' }).recovery, /coderai --setup/);
  assert.match(recoveryNotice({ code: 'context.overflow', message: 'Too much input' }).recovery, /\/compact/);
  assert.match(recoveryNotice({ code: 'model.not_found', message: 'Unknown model' }).recovery, /\/model/);
  assert.match(recoveryNotice({ code: 'provider.rate_limit', message: 'Busy' }).recovery, /limit to reset/);
  assert.match(recoveryNotice({ code: 'provider.connection_error', message: 'Offline' }).recovery, /connection and provider endpoint/);
  assert.match(recoveryNotice(new Error('Unexpected failure')).recovery, /\/export-debug/);
  assert.equal(recoveryNotice(Object.assign(new Error('Aborted'), { name: 'AbortError' })).cancelled, true);
  assert.equal(recoveryNotice({ message: '{"error":{"message":"Invalid test credential"}}' }).message, 'Invalid test credential');
  assert.doesNotMatch(recoveryNotice(new Error('before\u001b]52;c;INJECT\u0007after')).message, /INJECT|52;c/);
});

test('approval diff highlights changes with surrounding context', () => {
  const before = 'one\ntwo\nold\nfour\nfive';
  const after = 'one\ntwo\nnew\nfour\nfive';
  const text = describeToolInput({ kind: 'diff', path: 'sample.txt', before, after });
  assert.match(text, /3\s+│ - old\n\s+3 │ \+ new/);
  assert.match(text, /2 2 │   two/);
  assert.match(text, /4 4 │   four/);
});

test('file mention searches nested files and additional directories', async t => {
  const root = await mkdtemp(join(tmpdir(), 'coderai-completion-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const cwd = join(root, 'main'), extra = join(root, 'extra');
  await mkdir(join(cwd, 'nested'), { recursive: true });
  await mkdir(extra);
  await writeFile(join(cwd, 'nested', 'needle.txt'), '');
  await writeFile(join(extra, 'needle-other.txt'), '');
  const provider = new CompletionProvider([], cwd, [extra]);
  const result = await provider.getSuggestions(['@needle'], 0, 7, { signal: new AbortController().signal });
  assert.ok(result.items.some(item => item.value === '@nested/needle.txt'));
  assert.ok(result.items.some(item => item.value === `@${join(extra, 'needle-other.txt')}`));
});

test('directory completion preserves relative prefixes and continues inside quoted directories', async t => {
  const root = await mkdtemp(join(tmpdir(), 'coderai-directory-completion-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, 'extra  space', 'child'), { recursive: true });
  const provider = new CompletionProvider([{ name: 'add-dir' }], root);
  const options = { signal: new AbortController().signal };
  const first = '/add-dir ./ex';
  const suggestion = await provider.getSuggestions([first], 0, first.length, options);
  const completed = provider.applyCompletion([first], 0, first.length, suggestion.items[0], suggestion.prefix);
  assert.equal(completed.lines[0], '/add-dir "./extra  space/"');
  assert.equal(completed.cursorCol, completed.lines[0].length - 1);
  const nested = await provider.getSuggestions(completed.lines, 0, completed.cursorCol, options);
  const child = provider.applyCompletion(completed.lines, 0, completed.cursorCol, nested.items[0], nested.prefix);
  assert.equal(child.lines[0], '/add-dir "./extra  space/child/"');
});

test('slash argument completions suggest supported actions without replacing free text', async () => {
  const provider = new CompletionProvider([
    { name: 'plan', description: 'Control plan mode' },
    { name: 'goal', description: 'Manage goals' },
  ], process.cwd());
  const signal = new AbortController().signal;
  const names = await provider.getSuggestions(['/pl'], 0, 3, { signal });
  assert.match(names.items[0].description, /\[on\|off\|show\|clear\]/);
  const options = await provider.getSuggestions(['/plan o'], 0, 7, { signal });
  assert.deepEqual(options.items.map(item => item.value), ['on', 'off']);
  assert.equal(await provider.getSuggestions(['/plan on'], 0, 8, { signal }), null);
  assert.equal(await provider.getSuggestions(['/goal replace do work'], 0, 21, { signal }), null);
});


test('undo choices only include user prompts after the last compaction', async () => {
  const { undoablePrompts } = await import('../apps/coderai/src/tui/undo.ts');
  const result = undoablePrompts([
    { role: 'user', content: 'old' },
    { role: 'user', origin: { kind: 'compaction_summary' }, content: 'summary' },
    { role: 'user', origin: { kind: 'injection' }, content: 'injected' },
    { role: 'user', content: 'new' },
  ]);
  assert.equal(result.stoppedAtCompaction, true);
  assert.deepEqual(result.prompts.map(message => message.content), ['new']);
});


test('custom themes inherit a base, ignore invalid colors, and can be reloaded', async t => {
  const { colors, listThemes, setTheme } = await import('../apps/coderai/src/tui/theme.ts');
  const home = await mkdtemp(join(tmpdir(), 'coderai-theme-'));
  t.after(async () => { await setTheme('dark', home); await rm(home, { recursive: true, force: true }); });
  await mkdir(join(home, 'themes'));
  const file = join(home, 'themes', 'ember.json');
  await writeFile(file, JSON.stringify({ name: 'ember', base: 'light', colors: { primary: '#123456', error: 'red', unknown: '#ffffff' } }));
  await writeFile(join(home, 'themes', 'dark.json'), '{}');
  assert.deepEqual(await listThemes(home), ['ember']);
  assert.equal(await setTheme('ember', home), true);
  assert.equal(colors.primary, '#123456');
  assert.equal(colors.error, '#A22020');
  await writeFile(file, JSON.stringify({ name: 'ember', colors: { primary: '#abcdef' } }));
  assert.equal(await setTheme('ember', home), true);
  assert.equal(colors.primary, '#abcdef');
  assert.equal(await setTheme('../ember', home), false);
  await writeFile(file, '{');
  assert.equal(await setTheme('ember', home), false);
  assert.equal(colors.primary, '#abcdef');
});

const { ConversationTranscript, ThinkingBlock } = await import('../apps/coderai/src/tui/transcript.ts');
const { previewDiff } = await import('../apps/coderai/src/tui/diff.ts');
const renderTranscript = (view, width = 100) => plain(view.render(width));
const tool = (id, path, agentId = 'main') => ({ agentId, toolCallId: id, name: 'Read', args: { path }, display: { kind: 'file_io', operation: 'read', path } });

test('diffs retain unchanged lines between separate edits and split distant hunks', () => {
  const before = Array.from({ length: 24 }, (_, i) => `line ${i + 1}`);
  const after = [...before]; after[1] = 'changed 2'; after[20] = 'changed 21';
  const diff = previewDiff(before.join('\n') + '\n', after.join('\n') + '\n');
  assert.equal(diff.match(/^@@ /gm).length, 2);
  assert.match(diff, /2\s+│ - line 2/);
  assert.match(diff, /\s+2 │ \+ changed 2/);
  assert.match(diff, /3\s+3 │   line 3/);
  assert.doesNotMatch(diff, /│ [-+] line 3\b/);
  assert.doesNotMatch(diff, /line 12\b/);
  assert.match(previewDiff('', 'new\n'), /@@ -0,0 \+1,1 @@/);
  assert.match(previewDiff('old\n', ''), /@@ -1,1 \+0,0 @@/);
  assert.match(previewDiff('same', 'same\n'), /No newline at end of file/);
  assert.equal(previewDiff('same', 'same'), '(No changes)');
  assert.match(previewDiff('', 'new\n'.repeat(1500)), /\+1,1500/);
  assert.match(previewDiff('old\n'.repeat(1200), 'new\n'.repeat(1200)), /preview omitted/);
});

test('diff edit paths preserve both inputs, including repeated lines', () => {
  const values = ['', 'a\n', 'b\na\n', 'a\nb\na\n', 'b\nb\na\nb\n', 'a\na\na\n'];
  for (const before of values) for (const after of values) {
    if (before === after) continue;
    const lines = previewDiff(before, after).split('\n').map(line => line.match(/│ ([-+ ]) (.*)$/)).filter(Boolean);
    assert.equal(lines.filter(line => line[1] !== '+').map(line => line[2] + '\n').join(''), before);
    assert.equal(lines.filter(line => line[1] !== '-').map(line => line[2] + '\n').join(''), after);
  }
});

test('thinking previews are bounded by rendered rows and remain expandable after completion', () => {
  const block = new ThinkingBlock();
  block.append('First secret\n' + 'long wrapped thought '.repeat(60) + '\nLast thought');
  assert.equal(block.render(30).length, 4);
  assert.match(plain(block.render(30)), /Last thought/);
  block.live = false;
  assert.equal(block.render(30).length, 4);
  assert.doesNotMatch(plain(block.render(30)), /Last thought/);
  assert.match(plain(block.render(30)), /First secret/);
  assert.match(plain(block.render(50)), /ctrl\+o to expand/);
  block.expanded = true;
  assert.match(plain(block.render(30)), /First secret/);
  assert.match(plain(block.render(30)), /Last thought/);
  for (const width of [1, 3, 20, 50, 100]) assert.ok(block.render(width).every(line => visibleWidth(line) <= width));
});

test('tool cards correlate out-of-order results by agent and call and update in place', () => {
  const transcript = new ConversationTranscript();
  transcript.toolStarted(tool('same', 'main.ts'));
  transcript.toolStarted(tool('same', 'worker.ts', 'worker'));
  transcript.toolResult({ agentId: 'worker', toolCallId: 'same', output: 'Worker failed details', isError: true });
  transcript.toolResult({ agentId: 'main', toolCallId: 'same', output: 'Main success' });
  const collapsed = renderTranscript(transcript);
  assert.match(collapsed, /Used Read \(main\.ts\) · 1 line/);
  assert.match(collapsed, /✗ Agent worker · Used Read \(worker\.ts\)/);
  assert.match(collapsed, /Worker failed details/);
  assert.doesNotMatch(collapsed, /Main success|running/);
  transcript.setExpanded(true);
  assert.match(renderTranscript(transcript), /Main success/);
  assert.equal(renderTranscript(transcript).match(/● Used Read/g).length, 1);
  transcript.toolStarted(tool('same', 'another-turn.ts'));
  transcript.finishTurn();
  assert.match(renderTranscript(transcript), /⊘ Interrupted Read \(another-turn\.ts\)/);
  transcript.clear();
  assert.equal(transcript.hasDetails, false);
  assert.equal(renderTranscript(transcript), '');
});

test('consecutive reads group, expose failures, and preserve every result when expanded', () => {
  const transcript = new ConversationTranscript();
  transcript.toolStarted(tool('1', 'one.ts')); transcript.toolStarted(tool('2', 'two.ts'));
  transcript.toolResult({ agentId: 'main', toolCallId: '2', output: 'Missing two', isError: true });
  assert.match(renderTranscript(transcript), /Reading 2 files… · 1 failed/);
  assert.match(renderTranscript(transcript), /two\.ts · failed/);
  transcript.toolResult({ agentId: 'main', toolCallId: '1', output: 'Contents of one' });
  assert.match(renderTranscript(transcript), /Read 2 files · 1 line · 1 failed/);
  assert.doesNotMatch(renderTranscript(transcript), /Contents of one/);
  transcript.setExpanded(true);
  assert.match(renderTranscript(transcript), /Contents of one/);
  transcript.delta('Answer');
  transcript.toolStarted(tool('3', 'three.ts'));
  assert.match(renderTranscript(transcript), /Using Read \(three\.ts\)/);
  assert.doesNotMatch(renderTranscript(transcript), /Read 3 files/);
  for (const width of [1, 3, 20, 50, 100]) assert.ok(transcript.render(width).every(line => visibleWidth(line) <= width));
});

test('live and replay use identical user, thinking, assistant and structured tool blocks', () => {
  const live = new ConversationTranscript(), replay = new ConversationTranscript();
  live.userMessage('**literal prompt**');
  live.thinkingDelta('Visible reasoning'); live.delta('Before tool');
  live.toolStarted(tool('read', 'file.ts'));
  live.toolResult({ agentId: 'main', toolCallId: 'read', output: '# literal tool output' });
  live.delta('Final **answer**'); live.finishTurn();
  const message = (role, content, extra = {}) => ({ role, content: [{ type: 'text', text: content }], toolCalls: [], ...extra });
  replay.replayMessage(message('user', '**literal prompt**'));
  replay.replayMessage(message('assistant', '', {
    content: [{ type: 'think', think: 'Visible reasoning' }, { type: 'think', think: 'HIDDEN', hidden: true }, { type: 'text', text: 'Before tool' }],
    toolCalls: [{ id: 'read', name: 'Read', arguments: '{"path":"file.ts"}' }],
    toolCallDisplays: { read: tool('read', 'file.ts').display },
  }));
  replay.replayMessage(message('tool', '# literal tool output', { toolCallId: 'read' }));
  replay.replayMessage(message('assistant', 'Final **answer**')); replay.finishTurn();
  assert.equal(renderTranscript(live), renderTranscript(replay));
  live.setExpanded(true); replay.setExpanded(true);
  assert.equal(renderTranscript(live), renderTranscript(replay));
  assert.match(renderTranscript(replay), /\*\*literal prompt\*\*/);
  assert.match(renderTranscript(replay), /# literal tool output/);
  assert.doesNotMatch(renderTranscript(replay), /HIDDEN/);
  assert.equal(replay.lastAssistantText, live.lastAssistantText);
  assert.equal(replay.lastAssistantText, 'Before toolFinal **answer**');
});

test('transcript sanitizes split sequences, keeps missing results visible and handles legacy tool arguments', () => {
  const transcript = new ConversationTranscript();
  transcript.thinkingDelta('Safe\u001b'); transcript.thinkingDelta(']52;c;THINKING_ATTACK\u0007 thought');
  transcript.delta('Answer\u001b'); transcript.delta(']52;c;ANSWER_ATTACK\u0007 safe');
  transcript.toolStarted(tool('pending', 'safe.ts\u001b]52;c;PATH_ATTACK\u0007'));
  transcript.toolProgress('main', 'pending', 'Progress\u001b');
  transcript.toolProgress('main', 'pending', ']52;c;PROGRESS_ATTACK\u0007 safe');
  transcript.setExpanded(true);
  assert.doesNotMatch(renderTranscript(transcript), /ATTACK/);
  transcript.finishTurn();
  assert.match(renderTranscript(transcript), /⊘ Interrupted Read/);
  transcript.replayMessage({ role: 'assistant', content: [], toolCalls: [{ name: 'Legacy', id: 'legacy', arguments: '{incomplete' }] });
  transcript.toolResult({ agentId: 'main', toolCallId: 'legacy', output: 'done' });
  assert.match(renderTranscript(transcript), /\{incomplete/);
  transcript.toolResult({ agentId: 'main', toolCallId: 'unknown', output: 'orphan result' });
  assert.match(renderTranscript(transcript), /orphan result/);
});

test('replayed failed todo tools retain their identity and error preview', () => {
  const transcript = new ConversationTranscript();
  transcript.replayMessage({ role: 'assistant', content: [], toolCalls: [{ id: 'todo', name: 'TodoList', arguments: '{}' }] });
  transcript.replayMessage({ role: 'tool', toolCallId: 'todo', isError: true, content: [{ type: 'text', text: 'Invalid task status' }], toolCalls: [] });
  assert.match(renderTranscript(transcript), /✗ Used TodoList/);
  assert.match(renderTranscript(transcript), /Invalid task status/);
});

test('tool progress replacements remove old output and workspace paths stay readable', () => {
  const transcript = new ConversationTranscript(); transcript.setWorkspace('/example/project');
  transcript.toolStarted(tool('read', '/example/project/src/file.ts'));
  transcript.toolProgress('main', 'read', 'Old progress');
  transcript.toolProgress('main', 'read', 'Current progress', true);
  assert.match(renderTranscript(transcript), /Using Read \(src\/file\.ts\)/);
  assert.doesNotMatch(renderTranscript(transcript), /example\/project|Old progress/);
  assert.match(renderTranscript(transcript), /Current progress/);
});

test('nested plugin and goal argument completions stop at exact matches so Enter submits', async () => {
  const provider = new CompletionProvider([{ name: 'goal' }, { name: 'plugins' }], process.cwd());
  const options = { signal: new AbortController().signal };
  assert.equal(await provider.getSuggestions(['/goal next list'], 0, 15, options), null);
  const result = await provider.getSuggestions(['/plugins mcp en'], 0, 15, options);
  assert.deepEqual(result.items.map(item => item.value), ['mcp enable']);
  assert.equal(await provider.getSuggestions(['/plugins mcp enable'], 0, 19, options), null);
});
