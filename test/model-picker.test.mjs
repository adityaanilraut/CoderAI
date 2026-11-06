import { test } from 'node:test';
import assert from 'node:assert/strict';
const { chooseModel } = await import('../apps/coderai/src/tui/model-picker.ts');

function setup(answers) {
  const choices = [], messages = [], validated = [], selected = [];
  const runtime = {
    harness: { getConfig: async () => ({ providers: { openai: { type: 'openai' }, deepseek: { type: 'openai' } }, models: {
      fast: { provider: 'openai', model: 'gpt-fast', displayName: 'GPT Fast' },
      reason: { provider: 'deepseek', model: 'deepseek-reasoner' },
    } }) },
    session: { getStatus: async () => ({ model: 'fast' }), setModel: async alias => selected.push(alias) },
    validateModel: async alias => validated.push(alias),
  };
  const choose = async (title, items) => { choices.push({ title, items }); return answers.shift() ?? ''; };
  const view = { choose, chooseSearchable: choose, message: text => messages.push(text) };
  return { runtime, view, choices, messages, validated, selected };
}

test('model picker groups configured models by provider and selects the alias', async () => {
  const f = setup(['openai', 'fast']);
  await chooseModel(f.runtime, f.view);
  assert.deepEqual(f.choices[0].items.map(item => item.label), ['DeepSeek', 'OpenAI', 'Add provider or model…']);
  assert.equal(f.choices[1].items[0].label, '[current] GPT Fast');
  assert.match(f.choices[1].items[0].description, /Current/);
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


test('model picker exposes context and capabilities and keeps setup available for providers without models', async () => {
  const f = setup(['empty', '\0back', 'openai', 'fast']);
  f.runtime.harness.getConfig = async () => ({ providers: { openai: { type: 'openai' }, empty: { type: 'anthropic' } }, models: {
    fast: { provider: 'openai', model: 'fast-id', displayName: 'Fast', maxContextSize: 128000, capabilities: ['image_in', 'thinking'] },
  } });
  await chooseModel(f.runtime, f.view);
  assert.match(f.choices[0].items.find(item => item.value === 'empty').description, /0 configured models/);
  assert.ok(f.choices[1].items.some(item => item.value === '\0setup' && item.pinned));
  assert.match(f.choices[3].items[0].description, /Context: 128,000.*Images.*Thinking/);
});

test('model picker refreshes the configuration after setup and selects a newly added model', async () => {
  const f = setup(['\0setup', 'openai', 'fresh']);
  let configured = false, reloaded = false;
  f.runtime.harness.getConfig = async () => ({ providers: { openai: { type: 'openai' } }, models: configured ? { fresh: { provider: 'openai', model: 'fresh-model' } } : {} });
  f.view.suspend = async () => { configured = true; };
  f.runtime.session.reloadSession = async () => { reloaded = true; };
  await chooseModel(f.runtime, f.view);
  assert.ok(reloaded); assert.deepEqual(f.selected, ['fresh']);
  assert.deepEqual(f.messages, ['Model: fresh-model']);
});

test('model picker shows video, audio and dynamic tool capability badges', async () => {
  const f = setup(['openai', 'media']);
  f.runtime.harness.getConfig = async () => ({ providers: { openai: { type: 'openai' } }, models: {
    media: { provider: 'openai', model: 'media-model', capabilities: ['video_in', 'audio_in', 'dynamically_loaded_tools'] },
  } });
  await chooseModel(f.runtime, f.view);
  assert.match(f.choices[1].items[0].description, /Video.*Audio.*Tool Select/);
});
