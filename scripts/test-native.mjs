import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { stdio: 'inherit', env });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run(process.execPath, ['scripts/build-bun.mjs']);
const binary = resolve('dist-bun', `${process.platform}-${process.arch}`, process.platform === 'win32' ? 'coderai.exe' : 'coderai');
run(process.execPath, ['test', 'test/native.test.mjs'], { ...process.env, CODERAI_TEST_BINARY: binary });

if (process.platform !== 'win32') {
  run(process.execPath, ['test', '--test-name-pattern', 'real PTY:', 'test/phase56-features.test.mjs'], { ...process.env, CODERAI_TEST_BINARY: binary });
}
