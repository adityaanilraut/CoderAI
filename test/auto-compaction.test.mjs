import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

for (const enabled of [true, false]) test(enabled ? 'automatic compaction completes before a resumed turn when the context fills' : 'disabled full compaction leaves resumed context untouched', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const requests = [];
  const server = createServer(async (req, res) => {
    let data = ''; for await (const chunk of req) data += chunk;
    requests.push(JSON.parse(data));
    const chunk = { id: 'compaction-fixture', object: 'chat.completion.chunk', created: 1, model: 'fixture-model', choices: [{ index: 0, delta: { role: 'assistant', content: 'Deterministic compaction reply.' }, finish_reason: null }] };
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write(`data: ${JSON.stringify(chunk)}\n\n`);
    chunk.choices = [{ index: 0, delta: {}, finish_reason: 'stop' }];
    const promptTokens = requests.length === 1 ? 4900 : 100;
    chunk.usage = { prompt_tokens: promptTokens, completion_tokens: 20, total_tokens: promptTokens + 20 };
    res.end(`data: ${JSON.stringify(chunk)}\n\ndata: [DONE]\n\n`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  f.env.CODERAI_MODEL_BASE_URL = `http://127.0.0.1:${server.address().port}/v1`;
  f.env.CODERAI_MODEL_MAX_CONTEXT_SIZE = '5000';
  await writeFile(join(f.home, 'config.toml'), 'telemetry = false\n[loop_control]\nreserved_context_size = 0\ncompaction_trigger_ratio = 0.5\n' + (enabled ? '' : '[experimental]\nfull_compaction = false\n'));
  const first = await f.run(['--trust', '-p', 'Remember this initial task.', '--json']);
  assert.equal(first.code, 0, first.stderr);
  const id = JSON.parse(first.stdout.split('\n')[0]).sessionId;
  const second = await f.run(['--resume', id, '-p', 'Continue the task.', '--json']);
  assert.equal(second.code, 0, second.stderr);
  const events = second.stdout.trim().split('\n').map(line => JSON.parse(line));
  if (enabled) {
  assert.ok(events.some(event => event.type === 'compaction.started' && event.trigger === 'auto'));
  assert.ok(events.some(event => event.type === 'compaction.completed' && event.result.compactedCount > 0));
  assert.ok(events.some(event => event.type === 'assistant.delta' && event.delta.includes('Deterministic compaction reply.')));
  assert.ok(requests.length >= 3, 'summary and subsequent turn reached the provider');
  } else {
    assert.ok(!events.some(event => event.type.startsWith('compaction.')));
    assert.equal(requests.length, 2);
    assert.ok(JSON.stringify(requests[1].messages).includes('Remember this initial task.'));
  }
});
