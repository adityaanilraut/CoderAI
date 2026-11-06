import { test } from 'node:test';
import assert from 'node:assert/strict';

const { headless } = await import('../apps/coderai/src/headless.ts');

test('plain headless output contains only main agent text, while JSON retains attribution', async () => {
  const session = { id: 'session', workDir: '/test', setStaleReloadHandler() {}, async getSessionWarnings() { return []; } };
  const runtime = {
    options: {}, session,
    open: async () => session,
    run: async (_prompt, emit) => {
      emit({ type: 'assistant.delta', agentId: 'main', delta: 'main ' });
      emit({ type: 'assistant.delta', agentId: 'child', delta: 'child' });
      emit({ type: 'assistant.delta', agentId: 'main', delta: 'answer' });
      return 'completed';
    },
  };
  const originalOut = process.stdout.write;
  const originalErr = process.stderr.write;
  let output = '';
  try {
    process.stdout.write = chunk => { output += chunk; return true; };
    process.stderr.write = () => true;
    assert.equal(await headless(runtime, 'test', false), 0);
    assert.equal(output, 'main answer\n');
    output = '';
    assert.equal(await headless(runtime, 'test', true), 0);
    const events = output.trim().split('\n').map(line => JSON.parse(line));
    assert.ok(events.some(event => event.agentId === 'child' && event.delta === 'child'));
    assert.ok(events.some(event => event.agentId === 'main' && event.delta === 'answer'));
  } finally {
    process.stdout.write = originalOut;
    process.stderr.write = originalErr;
  }
});
