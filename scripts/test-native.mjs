import { spawnSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { unpackNativeArchive, verifyArchiveChecksum } from './release-archive.mjs';

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { stdio: 'inherit', env });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} ${args.join(' ')} failed: ${result.signal ?? result.status}`);
}

run(process.execPath, ['scripts/build-bun.mjs']);
const archive = resolve('dist-bun', `coderai-bun-${process.platform}-${process.arch}.zip`);
await verifyArchiveChecksum(archive);
const root = await mkdtemp(resolve(tmpdir(), 'coderai-standalone-'));
try {
  const binary = await unpackNativeArchive(archive, root, process.platform === 'win32' ? 'coderai.exe' : 'coderai');
  const env = { ...process.env, CODERAI_TEST_BINARY: binary };
  run(process.execPath, ['test', 'test/native.test.mjs'], env);
  if (process.platform !== 'win32') {
    run(process.execPath, ['test', '--test-name-pattern', 'real PTY:', 'test/phase56-features.test.mjs'], env);
  }
} finally {
  await rm(root, { recursive: true, force: true, maxRetries: 10 });
}
