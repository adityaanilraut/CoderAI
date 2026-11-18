import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { Readable } from 'node:stream';
import { test } from 'node:test';
import { ZipFile } from 'yazl';
import { fixture } from './fixture.mjs';

async function archive(entryCount = 0) {
  const zip = new ZipFile();
  zip.addBuffer(Buffer.from(JSON.stringify({ name: 'bounded-fixture', description: 'Fixture plugin' })), 'kimi.plugin.json');
  for (let index = 0; index < entryCount; index++) {
    zip.addReadStreamLazy(`file-${index}.txt`, { size: 0, compress: false },
      callback => queueMicrotask(() => callback(null, Readable.from([]))));
  }
  zip.end();
  const chunks = [];
  for await (const chunk of zip.outputStream) chunks.push(chunk);
  return Buffer.concat(chunks);
}

test('plugin ZIP downloads apply size and entry limits and clean temporary files', { timeout: 45000 }, async t => {
  const f = await fixture();
  t.after(f.cleanup);
  Object.assign(f.env, { TMPDIR: f.root, TMP: f.root, TEMP: f.root });
  const small = await archive();
  const many = await archive(10_000);
  const server = createServer((request, response) => {
    if (request.url === '/large.zip') {
      response.writeHead(200, { 'content-length': 64 * 1024 * 1024 + 1 });
      response.flushHeaders();
    } else {
      response.end(request.url === '/many.zip' ? many : small);
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  const url = `http://127.0.0.1:${server.address().port}`;
  const runInstall = filename => f.run(['plugins', 'install', `${url}/${filename}`]);

  const large = await runInstall('large.zip');
  assert.equal(large.code, 1, large.stdout + large.stderr);
  assert.match(large.stderr, /download exceeds 64 MiB/i);
  const manyEntries = await runInstall('many.zip');
  assert.equal(manyEntries.code, 1, manyEntries.stdout + manyEntries.stderr);
  assert.match(manyEntries.stderr, /exceeds 10,000 entries/i);
  assert.ok((await readdir(f.root)).every(name => !name.startsWith('kimi-plugin-zip-')));

  const result = await runInstall('valid.zip');
  assert.equal(result.code, 0, result.stdout + result.stderr);
  assert.match(result.stdout, /bounded-fixture/);
  assert.ok((await readdir(f.root)).every(name => !name.startsWith('kimi-plugin-zip-')));
});
