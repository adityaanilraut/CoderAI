import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { zstdCompressSync } from 'node:zlib';
import { readJsonLineBuffers } from '../packages/session-import/src/jsonl.ts';

test('session import limits JSONL records to 32 MiB, excluding the line terminator', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-import-limits-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'import.jsonl');
  const bytes = 32 * 1024 * 1024;
  const record = Buffer.alloc(bytes + 2, 32);
  record.write('\r\n', bytes);
  await writeFile(file, record);
  const records = await Array.fromAsync(readJsonLineBuffers(file));
  assert.equal(records.length, 1);
  assert.equal(records[0].length, bytes + 2);

  record[bytes] = 32;
  await writeFile(file, record);
  await assert.rejects(Array.fromAsync(readJsonLineBuffers(file)), { code: 'IMPORT_SIZE_LIMIT' });
});

test('session import counts decompressed bytes against the shared input budget', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-import-decode-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'import.jsonl.zst');
  const record = Buffer.from(JSON.stringify({ fixture: 'x'.repeat(1024) }) + '\n');
  await writeFile(file, zstdCompressSync(record));
  const budget = { remaining: record.length * 2 - 1 };
  assert.deepEqual(await Array.fromAsync(readJsonLineBuffers(file, budget)), [record]);
  await assert.rejects(Array.fromAsync(readJsonLineBuffers(file, budget)), { code: 'IMPORT_SIZE_LIMIT' });
});
