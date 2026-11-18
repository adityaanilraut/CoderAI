import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { releaseKind } from '../scripts/release-version.mjs';

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
  assert.match(formula, /prefix\.install "THIRD_PARTY_LICENSES\.txt"/);
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

test('release preflight distinguishes stable versions from prereleases', () => {
  assert.equal(releaseKind('v0.6.0', '0.6.0'), 'stable');
  assert.equal(releaseKind('v0.7.0-rc.1', '0.7.0-rc.1'), 'prerelease');
  assert.throws(() => releaseKind('v0.6.1', '0.6.0'), /does not match/);
  assert.throws(() => releaseKind('v0.6.0+build', '0.6.0+build'), /without build metadata/);
});

test('Homebrew publication retries after the default branch advances', { skip: process.platform === 'win32' }, async t => {
  const root = await mkdtemp(join(tmpdir(), 'coderai-tap-retry-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const origin = join(root, 'origin.git');
  const publish = join(root, 'publisher');
  const racer = join(root, 'racer');
  const bin = join(root, 'bin');
  const formula = join(root, 'generated.rb');
  const script = resolve('scripts/publish-homebrew.sh');
  const git = (cwd, ...args) => {
    const result = spawnSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', ...args], { cwd, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim();
  };
  git(root, 'init', '--bare', '--initial-branch=main', origin);
  git(root, 'clone', origin, publish);
  await mkdir(join(publish, 'Formula'));
  await writeFile(join(publish, 'Formula', 'README.md'), 'Initial formula directory');
  git(publish, 'add', 'Formula');
  git(publish, 'commit', '-m', 'Initial checkout');
  git(publish, 'push', 'origin', 'main');
  git(root, 'clone', origin, racer);
  await mkdir(bin);
  await writeFile(join(bin, 'gh'), [
    '#!/bin/sh',
    'set -eu',
    'if [ ! -e "$CODERAI_TAP_MARKER" ]; then',
    '  touch "$CODERAI_TAP_MARKER"',
    '  git -C "$CODERAI_TAP_RACER" -c user.name=Fixture -c user.email=fixture@example.test commit --quiet --allow-empty -m "Concurrent default branch commit"',
    '  git -C "$CODERAI_TAP_RACER" push --quiet origin main',
    'fi',
    'printf "%s\\n" v0.6.0',
    '',
  ].join('\n'), { mode: 0o755 });
  await writeFile(formula, 'Fixture formula\n');
  const env = { ...process.env, PATH: `${bin}:${process.env.PATH}`, CODERAI_TAP_MARKER: join(root, 'advanced'), CODERAI_TAP_RACER: racer };
  const run = () => spawnSync('bash', [script, 'v0.6.0', 'main', formula], { cwd: publish, env, encoding: 'utf8' });
  const result = run();
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.match(result.stderr, /non-fast-forward|fetch first/);
  assert.equal(git(origin, 'show', 'main:Formula/coderai.rb'), 'Fixture formula');
  assert.equal(git(origin, 'log', '-2', '--format=%s', 'main'), 'Update Homebrew formula to v0.6.0\nConcurrent default branch commit');
  const head = git(origin, 'rev-parse', 'main');
  assert.equal(run().status, 0);
  assert.equal(git(origin, 'rev-parse', 'main'), head);
});
