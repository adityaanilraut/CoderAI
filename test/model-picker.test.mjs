import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'tsx/esm/api';
register();
const { chooseModel } = await import('../apps/coderai/src/tui/model-picker.ts');

function setup(answers) {
  const choices = [], messages = [], validated = [], selected = [];
  const runtime = {
    harness: { getConfig: async () => ({ providers: { openai: { type: 'openai' }, deepseek: { type: 'openai' } }, models: {
      fast: { provider: 'openai', model: 'gpt-fast', displayName: 'GPT Fast' },
      reason: { provider: 'deepseek', model: 'deepseek-reasoner' },
    } }) },
    session: { setModel: async alias => selected.push(alias) },
    validateModel: async alias => validated.push(alias),
  };
  const view = { choose: async (title, items) => { choices.push({ title, items }); return answers.shift() ?? ''; }, message: text => messages.push(text) };
  return { runtime, view, choices, messages, validated, selected };
}

test('model picker groups configured models by provider and selects the alias', async () => {
  const f = setup(['openai', 'fast']);
  await chooseModel(f.runtime, f.view);
  assert.deepEqual(f.choices[0].items.map(item => item.label), ['DeepSeek', 'OpenAI']);
  assert.equal(f.choices[1].items[0].label, 'GPT Fast');
  assert.deepEqual(f.validated, ['fast']);
  assert.deepEqual(f.selected, ['fast']);
});

test('model picker can return to providers and cancel without changing model', async () => {
  const f = setup(['openai', '\0back', 'deepseek', '']);
  await chooseModel(f.runtime, f.view);
  assert.equal(f.choices.length, 4);
  assert.equal(f.choices[3].items[0].label, 'deepseek-reasoner');
  assert.deepEqual(f.selected, []);
});

