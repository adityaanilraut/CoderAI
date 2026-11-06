import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, readdir, stat, appendFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const { ChoiceDialog, PromptDialog, DocumentDialog } = await import('../apps/coderai/src/tui/dialogs.ts');
const { SessionPicker, relativeSessionTime, browseSessions } = await import('../apps/coderai/src/tui/session-picker.ts');
const { PromptHistory, mergePromptHistory } = await import('../apps/coderai/src/tui/prompt-history.ts');
const { groupedHelp } = await import('../apps/coderai/src/tui/help.ts');
const help = (await readFile(new URL('../apps/coderai/src/tui/commands.ts', import.meta.url), 'utf8')).split('export const help = `')[1].split('`;')[0];
const { Editor } = await import('../packages/pi-tui/src/components/editor.ts');
const { editorTheme } = await import('../apps/coderai/src/tui/theme.ts');
const { stripTerminalSequences, visibleWidth } = await import('../packages/pi-tui/src/utils.ts');
const plain = component => component.map(stripTerminalSequences).join('\n');
const options = Array.from({ length: 30 }, (_, i) => ({ value: String(i), label: `Option ${i}`, description: `Details for option ${i}. ` .repeat(8) }));

function dialog(items = options, opts = {}) {
  const answers = [];
  const picker = new ChoiceDialog('Choose something:', items, () => 16, answer => answers.push(answer), () => {}, opts);
  return { picker, answers };
}

test('choice dialogs search, recover from no matches, page, and preserve selection on unchanged query', () => {
  const { picker, answers } = dialog(options, { searchable: true });
  picker.render(50); picker.handleInput('Option 2'); picker.render(50);
  picker.handleInput('\u001b[B');
  assert.equal(picker.selection.value, '20');
  picker.handleInput('\u001b[D');
  assert.equal(picker.selection.value, '20');
  picker.handleInput('\u0015'); picker.handleInput('\u000b'); picker.handleInput('no match');
  assert.match(plain(picker.render(50)), /No matching options/);
  picker.handleInput('\r'); assert.deepEqual(answers, []);
  picker.handleInput('\u0015'); picker.handleInput('Option 29');
  picker.handleInput('\r'); assert.deepEqual(answers, ['29']);
  const paging = dialog(); paging.picker.render(50); paging.picker.handleInput('\u001b[6~');
  assert.notEqual(paging.picker.selection.value, '0');
  paging.picker.handleInput('\u001b[F'); assert.equal(paging.picker.selection.value, '29');
});

test('long previews scroll independently while choices stay visible and dialogs fit small terminals', () => {
  for (const [width, rows] of [[88, 32], [50, 16], [38, 16], [20, 12]]) {
    const picker = new ChoiceDialog('Review plan\n' + Array.from({ length: 80 }, (_, i) => `Plan line ${i}`).join('\n'),
      [{ value: 'approve', label: 'Approve' }, { value: 'reject', label: 'Reject' }], () => rows, () => {}, () => {});
    const before = plain(picker.render(width)); picker.handleInput('\u001b[6~');
    const after = picker.render(width);
    assert.notEqual(plain(after), before);
    assert.match(plain(after), /Approve/);
    assert.match(plain(after), /Reject/);
    assert.ok(after.length <= rows - 2, `${width}x${rows}: ${after.length}`);
    assert.ok(after.every(line => visibleWidth(line) <= width));
  }
});

test('multi-select supports Space and Enter, requires a selection, and returns checked items in order', () => {
  const { picker, answers } = dialog([{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }, { value: '\0done', label: 'Done' }], { multiple: true });
  picker.render(50); picker.handleInput('\u001b[F'); picker.handleInput('\r');
  assert.deepEqual(answers, []);
  assert.match(plain(picker.render(50)), /Select at least one/);
  picker.handleInput('\u001b[H'); picker.handleInput(' '); picker.handleInput('\u001b[B'); picker.handleInput('\r');
  assert.deepEqual(picker.values, ['a', 'b']);
  picker.handleInput('\u001b[F'); picker.handleInput('\r');
  assert.deepEqual(answers, ['\0done']);
});

test('prompt dialogs edit prefilled values at the end and keep large questions scrollable', () => {
  const answers = [];
  const prompt = new PromptDialog('Rename\n' + 'Long question\n'.repeat(40), () => 16, answer => answers.push(answer), () => {}, 'Old title');
  prompt.handleInput(' revised'); prompt.handleInput('\r');
  assert.deepEqual(answers, ['Old title revised']);
  const before = plain(prompt.render(38)); prompt.handleInput('\u001b[6~');
  assert.notEqual(plain(prompt.render(38)), before);
  assert.match(plain(prompt.render(38)), /Reply:/);
});

test('help contains each command once, scrolls to the last section, and sanitizes terminal text', () => {
  const text = groupedHelp(help);
  for (const command of help.match(/^\/\S+/gm)) assert.equal(text.split(command + ' ').length - 1, 1, command);
  assert.match(text, /CONVERSATIONS/); assert.match(text, /MODELS & SETTINGS/);
  const doc = new DocumentDialog('Help', text + '\nSafe\u001b]52;c;ATTACK\u0007 end', () => 16, () => {}, () => {});
  const before = plain(doc.render(50)); doc.handleInput('\u001b[F');
  assert.notEqual(plain(doc.render(50)), before); assert.match(plain(doc.render(50)), /Safe end/);
  assert.doesNotMatch(doc.render(50).join('\n'), /ATTACK/);
});

const sessions = [
  { id: 'here', title: 'Local conversation', workDir: '/project', updatedAt: Date.now(), createdAt: 1 },
  { id: 'away', title: 'Other conversation', workDir: '/other', updatedAt: Date.now() - 3600000, createdAt: 1 },
];
test('session browser shows current workspace, toggles scope, searches and emits management actions', () => {
  const answers = [];
  const picker = new SessionPicker(sessions, { id: 'here', workDir: '/project' }, { all: false }, () => 16, answer => answers.push(answer), () => {});
  assert.match(plain(picker.render(50)), /current/); assert.doesNotMatch(plain(picker.render(50)), /Other conversation/);
  picker.handleInput('\u0001'); assert.match(plain(picker.render(50)), /All workspaces/);
  picker.handleInput('Other'); picker.handleInput('\u0012');
  assert.deepEqual(JSON.parse(answers[0]), { action: 'rename', id: 'away' });
  picker.handleInput('\u0018'); assert.deepEqual(JSON.parse(answers[1]), { action: 'delete', id: 'away' });
  assert.equal(relativeSessionTime(Date.now() + 1000), 'Just now');
  assert.equal(relativeSessionTime(NaN), 'Unknown time');
});

test('session deletion requires confirmation and never deletes the open session', async () => {
  const deleted = [], notices = [], choices = [];
  const responses = [JSON.stringify({ action: 'delete', id: 'here' }), JSON.stringify({ action: 'delete', id: 'away' }), JSON.stringify({ action: 'delete', id: 'away' }), ''];
  const decisions = ['keep', 'delete'];
  const runtime = { session: { id: 'here', workDir: '/project' }, harness: { listSessions: async () => sessions, deleteSession: async id => deleted.push(id) } };
  const view = { showDialog: async () => responses.shift(), notice: text => notices.push(text), choose: async (_title, items) => { choices.push(items); return decisions.shift(); }, message() {} };
  assert.equal(await browseSessions(runtime, view), '');
  assert.deepEqual(deleted, ['away']); assert.match(notices[0], /conversation is open/);
  assert.equal(choices[0][0].value, 'keep');
});

test('prompt history persists per workspace, merges concurrent writers, ignores damaged entries and stays bounded', async t => {
  const root = await mkdtemp(join(tmpdir(), 'coderai-history-')); t.after(() => rm(root, { recursive: true, force: true }));
  const a = join(root, 'a'), b = join(root, 'b'); await mkdir(a); await mkdir(b);
  const store = new PromptHistory(root), other = new PromptHistory(root);
  await Promise.all([store.append(a, 'First\nmultiline prompt'), other.append(a, 'Second prompt')]);
  assert.deepEqual(new Set(await store.load(a)), new Set(['First\nmultiline prompt', 'Second prompt']));
  await store.append(b, 'Other workspace'); assert.deepEqual(await store.load(b), ['Other workspace']);
  const files = await readdir(join(root, 'input-history'));
  const file = join(root, 'input-history', files[0]);
  await appendFile(file, 'not-json\n{"content":null}\n');
  assert.deepEqual(new Set(await store.load(a)), new Set(['First\nmultiline prompt', 'Second prompt']));
  assert.deepEqual(await store.load(b), ['Other workspace']);
  for (let i = 0; i < 104; i++) await store.append(a, `Prompt ${i}`);
  const saved = await new PromptHistory(root).load(a);
  assert.equal(saved.length, 100); assert.equal(saved[0], 'Prompt 4'); assert.equal(saved.at(-1), 'Prompt 103');
  assert.equal((await stat(file)).mode & 0o077, 0);
  assert.deepEqual(mergePromptHistory(['A', 'B'], ['A', 'B', 'C']), ['A', 'B', 'C']);
});

test('editor replaces workspace history without retaining prompts from the previous workspace', () => {
  const editor = new Editor({ requestRender() {} }, editorTheme);
  editor.setHistory(['old workspace']); editor.handleInput('\u001b[A'); assert.equal(editor.getText(), 'old workspace');
  editor.setText('draft'); editor.setHistory(['older prompt', 'new workspace']); assert.equal(editor.getText(), 'draft');
  editor.setText(''); editor.handleInput('\u001b[A'); assert.equal(editor.getText(), 'new workspace');
  editor.handleInput('\u001b[A'); assert.equal(editor.getText(), 'older prompt');
  editor.handleInput('\u001b[A'); assert.equal(editor.getText(), 'older prompt');
  editor.setHistory([]); editor.setText(''); editor.handleInput('\u001b[A'); assert.equal(editor.getText(), '');
});

test('dialog input filters pasted terminal sequences before displaying or submitting them', () => {
  const { picker } = dialog(options, { searchable: true });
  picker.handleInput('\u001b[200~Option\u001b]52;c;SEARCH_ATTACK\u0007 2\u001b[201~');
  assert.doesNotMatch(picker.render(50).join('\n'), /SEARCH_ATTACK|52;c/);
  assert.equal(picker.selection.value, '2');
  const answers = [];
  const prompt = new PromptDialog('Answer', () => 16, answer => answers.push(answer), () => {});
  prompt.handleInput('\u001b[200~Safe\u001b]52;c;PROMPT_ATTACK\u0007 answer\u001b[201~');
  assert.doesNotMatch(prompt.render(50).join('\n'), /PROMPT_ATTACK|52;c/);
  prompt.handleInput('\r'); assert.deepEqual(answers, ['Safe answer']);
});
