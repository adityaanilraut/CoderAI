import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { register } from 'tsx/esm/api';
register();
const { describeToolInput } = await import('../apps/coderai/src/tui/display.ts');
const { CompletionProvider } = await import('../apps/coderai/src/tui/completion.ts');

test('approval diff highlights changes with surrounding context', () => {
  const before = 'one\ntwo\nold\nfour\nfive';
  const after = 'one\ntwo\nnew\nfour\nfive';
  const text = describeToolInput({ kind: 'diff', path: 'sample.txt', before, after });
  assert.match(text, /- old\n\+ new/);
  assert.match(text, /  two/);
  assert.match(text, /  four/);
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
