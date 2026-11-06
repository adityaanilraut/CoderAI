import { readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';

const files = readdirSync('test').filter(name => name.endsWith('.test.mjs')).sort().map(name => join('test', name));
const result = spawnSync(process.execPath, ['test', ...files], {
  stdio: 'inherit',
  env: { ...process.env, CODERAI_TEST_RUNTIME: process.env.CODERAI_TEST_RUNTIME || 'bun' },
});
if (result.error) throw result.error;
process.exit(result.status ?? 1);
