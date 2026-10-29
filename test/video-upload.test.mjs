import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { open } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('large video uses Kimi file upload and survives session resume', { timeout: 60000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  let uploadCount = 0; let uploadedBytes = 0; const requests = [];
  const server = createServer(async (req, res) => {
    if (req.url === '/v1/files') {
      uploadCount++;
      for await (const chunk of req) uploadedBytes += chunk.length;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ id: 'file-video-test', object: 'file', bytes: uploadedBytes, created_at: 1, filename: 'large.mp4', purpose: 'video' }));
      return;
    }
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString()); requests.push(body);
    const chunk = { id: 'fixture', object: 'chat.completion.chunk', created: 1, model: body.model, choices: [{ index: 0, delta: { role: 'assistant', content: 'Video received.' }, finish_reason: null }] };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.end(`data: ${JSON.stringify(chunk)}\n\ndata: ${JSON.stringify({ ...chunk, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\ndata: [DONE]\n\n`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  Object.assign(f.env, { CODERAI_MODEL_PROVIDER_TYPE: 'kimi', CODERAI_MODEL_CAPABILITIES: 'video_in', CODERAI_MODEL_BASE_URL: `http://127.0.0.1:${server.address().port}/v1` });
  const file = await open(join(f.cwd, 'large.mp4'), 'w');
  await file.write(Buffer.from([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]));
  await file.truncate(21 * 1024 * 1024); await file.close();
  const first = await f.run(['--trust', '--json', '-p', 'Inspect video', '--attach', 'large.mp4']);
  assert.equal(first.code, 0, first.stderr);
  assert.equal(uploadCount, 1); assert.ok(uploadedBytes > 20 * 1024 * 1024);
  assert.match(JSON.stringify(requests[0]), /ms:\/\/file-video-test/);
  const id = JSON.parse(first.stdout.split('\n')[0]).sessionId;
  const second = await f.run(['--resume', id, '-p', 'Continue']);
  assert.equal(second.code, 0, second.stderr);
  assert.match(JSON.stringify(requests[1]), /ms:\/\/file-video-test/);
  assert.equal(uploadCount, 1);
  const oversized = await open(join(f.cwd, 'oversized.mp4'), 'w');
  await oversized.write(Buffer.from([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]));
  await oversized.truncate(100 * 1024 * 1024 + 1); await oversized.close();
  const rejected = await f.run(['-p', 'Inspect', '--attach', 'oversized.mp4']);
  assert.equal(rejected.code, 1); assert.match(rejected.stderr, /100 MiB/);
  assert.equal(uploadCount, 1);
});
