import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { copyFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const guardSource = new URL('./runtime-guard.cjs', import.meta.url);

export async function guardedRuntimeEnv(fixture, forbiddenPath) {
  const relativeGuard = './coderai-runtime-guard.cjs';
  await copyFile(guardSource, join(fixture.cwd, relativeGuard));
  const canary = forbiddenPath ?? join(fixture.root, 'guard-canary.txt');
  if (!forbiddenPath) await writeFile(canary, 'guard canary');

  const env = {
    ...fixture.env,
    BUN_OPTIONS: `--preload=${relativeGuard}`,
    NODE_OPTIONS: '',
    CODERAI_FORBIDDEN_CHECKOUT: canary,
  };
  const program = `
    import assert from 'node:assert/strict';
    import { readFile } from 'node:fs/promises';
    import { createServer } from 'node:net';
    assert.throws(() => createServer().listen(0).unref(), /Unexpected product listening socket/);
    await assert.rejects(async () => readFile(process.env.CODERAI_FORBIDDEN_CHECKOUT), /Forbidden upstream/);
    console.log('guard active');
  `;
  const result = await exec(process.execPath, ['-e', program], { cwd: fixture.cwd, env, timeout: 5000 });
  assert.equal(result.stdout.trim(), 'guard active', result.stderr);
  return env;
}
