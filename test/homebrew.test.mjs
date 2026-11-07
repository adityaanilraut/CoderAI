import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const targets = ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64'];
async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'coderai-homebrew-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const checksums = [];
  for (const target of targets) {
    const name = `coderai-bun-${target}.zip`;
    const contents = Buffer.from(`test archive for ${target}`);
    const checksum = createHash('sha256').update(contents).digest('hex');
    checksums.push(checksum);
    await writeFile(join(root, name), contents);
    await writeFile(join(root, `${name}.sha256`), `${checksum}  ${name}\n`);
  }
  const output = join(root, 'Formula', 'coderai.rb');
  return { root, output, checksums, run: (...args) => spawnSync(process.execPath, [
    resolve('scripts/build-homebrew.mjs'), '--assets-dir', root, '--output', output, ...args,
  ], { encoding: 'utf8' }) };
}

test('Homebrew formula maps all four platforms to verified versioned release assets', async t => {
  const f = await fixture(t);
  const result = f.run('--version', '0.6.0');
  assert.equal(result.status, 0, result.stderr);
  const formula = await readFile(f.output, 'utf8');
  for (const [i, target] of targets.entries()) {
    assert.ok(formula.includes(`releases/download/v0.6.0/coderai-bun-${target}.zip"\n      sha256 "${f.checksums[i]}"`));
  }
  assert.match(formula, /version "0\.6\.0"/);
  assert.match(formula, /depends_on "ripgrep"/);
  assert.doesNotMatch(formula, /depends_on "(?:bun|node)"/);
});

test('Homebrew generation rejects corrupted archives before writing a formula', async t => {
  const f = await fixture(t);
  await writeFile(join(f.root, 'coderai-bun-linux-x64.zip'), 'corrupted');
  const result = f.run();
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Checksum mismatch for coderai-bun-linux-x64.zip/);
  await assert.rejects(readFile(f.output), { code: 'ENOENT' });
});

test('Homebrew generation rejects checksums for a different archive', async t => {
  const f = await fixture(t);
  await writeFile(join(f.root, 'coderai-bun-darwin-arm64.zip.sha256'), `${f.checksums[0]}  other.zip\n`);
  const result = f.run();
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Invalid checksum file/);
});

test('Homebrew generation requires every supported platform', async t => {
  const f = await fixture(t);
  await rm(join(f.root, 'coderai-bun-linux-arm64.zip'));
  const result = f.run();
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ENOENT/);
  await assert.rejects(readFile(f.output), { code: 'ENOENT' });
});

test('Homebrew generation accepts only stable numeric release versions', async t => {
  const f = await fixture(t);
  for (const version of ['0.6.0-beta.1', 'v0.6.0', '0.6.0"\nmalicious']) {
    const result = f.run('--version', version);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Homebrew requires a stable release version/);
  }
});
