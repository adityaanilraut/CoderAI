import { spawnSync } from 'node:child_process';
import { mkdir, writeFile, readFile, copyFile, chmod } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { ZipFile } from 'yazl';

// --build-sea embeds the JavaScript bundle and assets into a copy of Node.
// Build on the target OS and architecture; Node does not cross-compile SEAs.
const [major] = process.versions.node.split('.').map(Number);
if (major < 26) throw new Error('Native builds require Node.js 26 or newer (the resulting executable includes Node).');
const target = `${process.platform}-${process.arch}`;
const executable = process.platform === 'win32' ? 'coderai.exe' : 'coderai';
const outputDir = resolve('dist-native', target);
const binary = join(outputDir, executable);
const run = (cmd, args) => {
  const result = spawnSync(cmd, args, { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${cmd} failed with status ${result.status}`);
};
await mkdir(outputDir, { recursive: true });
run(process.execPath, ['scripts/build.mjs', '--native']);
const config = resolve('dist-native/sea.json');
const packageRequire = createRequire(import.meta.url);
const clipboardRequire = createRequire(packageRequire.resolve('@mariozechner/clipboard'));
const clipboardTargets = process.platform === 'darwin' ? [`darwin-${process.arch}`, 'darwin-universal']
  : process.platform === 'win32' ? [`win32-${process.arch}-msvc`]
  : process.platform === 'linux' ? [`linux-${process.arch}-${process.report.getReport().header.glibcVersionRuntime ? 'gnu' : 'musl'}`] : [];
let clipboardBinary;
for (const targetName of clipboardTargets) {
  try { clipboardBinary = clipboardRequire.resolve(`@mariozechner/clipboard-${targetName}`); break; } catch { /* Try the next target. */ }
}
if (!clipboardBinary && clipboardTargets.length) throw new Error(`No native clipboard binary available for ${target}.`);
await writeFile(config, JSON.stringify({
  main: resolve('dist-native/main.cjs'), output: binary,
  assets: { 'text-build-worker.mjs': resolve('dist-native/text-build-worker.mjs'), ...(clipboardBinary ? { 'clipboard.node': clipboardBinary } : {}) },
  disableExperimentalSEAWarning: true,
}, null, 2));
run(process.execPath, [`--build-sea=${config}`]);
if (process.platform === 'darwin') run('codesign', ['--force', '--sign', process.env.CODERAI_SIGNING_IDENTITY || '-', binary]);
if (process.platform !== 'win32') await chmod(binary, 0o755);
run(binary, ['--help']);
const licenseFiles = [['LICENSE', 'LICENSE'], ['NOTICE', 'NOTICE'], ['packages/pi-tui/LICENSE', 'LICENSE.pi-tui'], ['third-party-licenses/jsquash-webp.LICENSE', 'LICENSE.jsquash-webp'], ['third-party-licenses/libwebp.LICENSE', 'LICENSE.libwebp']];
const zip = new ZipFile();
zip.addFile(binary, executable, { mode: 0o100755 });
for (const [src, name] of licenseFiles) { await copyFile(src, join(outputDir, name)); zip.addFile(src, name); }
zip.end();
const archive = resolve('dist-native', `coderai-${target}.zip`);
await pipeline(zip.outputStream, createWriteStream(archive));
const digest = createHash('sha256').update(await readFile(archive)).digest('hex');
await writeFile(`${archive}.sha256`, `${digest}  coderai-${target}.zip\n`);
console.log(`Built ${binary} and ${archive}`);
