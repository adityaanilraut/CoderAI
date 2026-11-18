import assert from 'node:assert/strict';
import { access, mkdir, mkdtemp, rm, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { MiniDb } from '../packages/minidb/src/index.ts';

test('MiniDb releases its writer lock when startup cleanup fails', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-minidb-retry-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const stale = join(dir, 'db.snapshot.tmp');
  // A directory in place of the old temp file makes the single-file cleanup fail.
  await mkdir(stale);
  await assert.rejects(MiniDb.open({ dir, valueCodec: 'json' }), error =>
    ['ERR_FS_EISDIR', 'EISDIR', 'EPERM'].includes(error.code));
  await assert.rejects(access(join(dir, 'db.lock')), { code: 'ENOENT' });
  await rmdir(stale);

  const db = await MiniDb.open({ dir, valueCodec: 'json' });
  try {
    await db.set('fixture', 'reopened');
    assert.equal(db.get('fixture'), 'reopened');
  } finally {
    await db.close();
  }
});
