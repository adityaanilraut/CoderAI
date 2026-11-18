import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { cli, fixture, testRuntime } from './fixture.mjs';

const sse = (event, value) => `${event ? `event: ${event}\n` : ''}data: ${JSON.stringify(value)}\n\n`;

test('retained provider adapters send a prompt and parse a streamed reply', { timeout: 45000 }, async t => {
  for (const provider of ['anthropic', 'google-genai', 'openai_responses']) {
    await t.test(provider, async t => {
      const f = await fixture(); t.after(f.cleanup);
      const requests = [];
      const server = createServer(async (req, res) => {
        let raw = ''; for await (const chunk of req) raw += chunk;
        requests.push({ path: req.url, headers: req.headers, body: JSON.parse(raw) });
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        if (provider === 'anthropic') {
          const message = { id: 'msg-fixture', type: 'message', role: 'assistant', model: 'fixture-model', content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 10, output_tokens: 0 } };
          res.write(sse('message_start', { type: 'message_start', message }));
          res.write(sse('content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }));
          res.write(sse('content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Provider fixture reply.' } }));
          res.write(sse('content_block_stop', { type: 'content_block_stop', index: 0 }));
          res.write(sse('message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 4 } }));
          res.write(sse('message_stop', { type: 'message_stop' }));
        } else if (provider === 'google-genai') {
          res.write(sse('', { candidates: [{ content: { role: 'model', parts: [{ text: 'Provider fixture reply.' }] }, finishReason: 'STOP' }], responseId: 'response-fixture' }));
          res.write(sse('', { usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 4 } }));
          res.write(sse('', { usageMetadata: { thoughtsTokenCount: 3, totalTokenCount: 17 } }));
        } else {
          const response = { id: 'resp-fixture', object: 'response', created_at: 1, status: 'completed', model: 'fixture-model', output: [], usage: { input_tokens: 10, output_tokens: 4, total_tokens: 14 } };
          res.write(sse('response.created', { type: 'response.created', response }));
          res.write(sse('response.output_text.delta', { type: 'response.output_text.delta', item_id: 'item-fixture', output_index: 0, content_index: 0, delta: 'Provider fixture reply.' }));
          res.write(sse('response.completed', { type: 'response.completed', response }));
        }
        res.end();
      });
      await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
      t.after(() => new Promise(resolve => server.close(resolve)));
      f.env.CODERAI_MODEL_PROVIDER_TYPE = provider;
      f.env.CODERAI_MODEL_CAPABILITIES = 'thinking';
      f.env.CODERAI_MODEL_BASE_URL = `http://127.0.0.1:${server.address().port}${provider === 'openai_responses' ? '/v1' : ''}`;
      const result = await f.run(['--trust', '-p', 'Provider fixture prompt', '--json']);
      assert.equal(result.code, 0, result.stderr);
      const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
      assert.ok(events.some(event => event.type === 'assistant.delta' && event.delta.includes('Provider fixture reply.')), result.stdout);
      if (provider === 'google-genai') {
        assert.ok(events.some(event => {
          const models = event.type === 'agent.status.updated' ? Object.values(event.usage?.byModel ?? {}) : [];
          return models.some(usage => usage.inputOther === 10 && usage.output === 7);
        }), result.stdout);
      }
      assert.ok(requests.length > 0);
      assert.match(JSON.stringify(requests[0].body), /Provider fixture prompt/);
      assert.match(requests[0].path, provider === 'anthropic' ? /messages/ : provider === 'google-genai' ? /streamGenerateContent/ : /responses/);
    });
  }
});

async function localProvider(t, provider, handler) {
  const f = await fixture();
  t.after(f.cleanup);
  const server = createServer((req, res) => {
    req.once('end', () => handler(res));
    req.resume();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {
    server.closeAllConnections();
    return new Promise(resolve => server.close(resolve));
  });
  f.env.CODERAI_MODEL_PROVIDER_TYPE = provider;
  f.env.CODERAI_MODEL_BASE_URL = `http://127.0.0.1:${server.address().port}${provider === 'openai' ? '/v1' : ''}`;
  return f;
}

test('headless reports incomplete stream termination as a failure', { timeout: 20000 }, async t => {
  const f = await localProvider(t, 'openai', res => {
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.end(sse('', {
      id: 'partial', object: 'chat.completion.chunk', model: 'fixture-model',
      choices: [{ index: 0, delta: { content: 'Some partial text.' }, finish_reason: null }],
    }));
  });
  f.env.KIMI_LOOP_MAX_ATTEMPTS_PER_STEP = '1';
  const result = await f.run(['--trust', '--json', '-p', 'Use the incomplete response fixture']);
  assert.equal(result.code, 1, result.stderr + result.stdout);
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).reason, 'failed');
  assert.match(result.stdout + result.stderr, /ended before reporting completion/);
});

test('headless cancellation disconnects a stalled Google response', { timeout: 20000, skip: process.platform === 'win32' }, async t => {
  const connected = Promise.withResolvers();
  const disconnected = Promise.withResolvers();
  let requests = 0;
  const f = await localProvider(t, 'google-genai', res => {
    requests++;
    res.once('close', () => disconnected.resolve());
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write(sse('', { candidates: [{ content: { role: 'model', parts: [{ text: 'First chunk' }] } }] }));
    connected.resolve();
  });
  const child = spawn(testRuntime, [cli, '--trust', '--json', '-p', 'Use the cancellation fixture'], {
    cwd: f.cwd, env: f.env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill(); });
  const closed = once(child, 'close');
  child.stderr.resume();
  let stdout = '';
  child.stdout.on('data', chunk => { stdout += chunk; });
  await connected.promise;
  child.kill('SIGTERM');
  const [code] = await closed;
  await disconnected.promise;
  assert.equal(code, 130, stdout);
  assert.equal(requests, 1);
  const events = stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).reason, 'cancelled');
});
