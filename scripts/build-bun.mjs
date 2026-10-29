import { spawnSync } from 'node:child_process';
import { mkdir, writeFile, copyFile, chmod, readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { createHash } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { ZipFile } from 'yazl';

const run = (cmd, args) => {
  const result = spawnSync(cmd, args, { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${cmd} failed with status ${result.status}`);
};
const target = `${process.platform}-${process.arch}`;
const executable = process.platform === 'win32' ? 'coderai.exe' : 'coderai';
const out = resolve('dist-bun', target);
await mkdir(out, { recursive: true });
run(process.execPath, ['scripts/build.mjs', '--bun-native']);
const require = createRequire(import.meta.url);
let clipboardRequire;
try { clipboardRequire = createRequire(require.resolve('@mariozechner/clipboard')); } catch { /* Clipboard support is optional. */ }
const targets = process.platform === 'darwin' ? [`darwin-${process.arch}`, 'darwin-universal']
  : process.platform === 'win32' ? [`win32-${process.arch}-msvc`]
  : process.platform === 'linux' ? [`linux-${process.arch}-${process.report.getReport().header.glibcVersionRuntime ? 'gnu' : 'musl'}`] : [];
let clipboard;
for (const name of targets) { try { clipboard = clipboardRequire?.resolve(`@mariozechner/clipboard-${name}`); break; } catch {} }
if (clipboardRequire && targets.length && !clipboard) throw new Error(`No native clipboard binary available for ${target}`);
if (clipboard) await copyFile(clipboard, 'dist-bun/clipboard.node');
const entry = `import worker from './text-build-worker.mjs' with { type: 'file' };
${clipboard ? "import clipboard from './clipboard.node' with { type: 'file' };" : ''}
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const dir = mkdtempSync(join(tmpdir(), 'coderai-bun-'));
const workerPath = join(dir, 'text-build-worker.mjs');
writeFileSync(workerPath, new Uint8Array(await Bun.file(worker).arrayBuffer()));
globalThis.__coderaiBunWorker = workerPath;
${clipboard ? "const clipboardPath = join(dir, 'clipboard.node');\nwriteFileSync(clipboardPath, new Uint8Array(await Bun.file(clipboard).arrayBuffer()));\nglobalThis.__coderaiBunClipboard = clipboardPath;" : ''}
process.on('exit', () => { try { rmSync(dir, { recursive: true, force: true }); } catch {} });
await import('./main.cjs');
`;
await writeFile('dist-bun/entry.ts', entry);
run('bun', ['build', '--compile', 'dist-bun/entry.ts', '--outfile', join(out, executable)]);
if (process.platform !== 'win32') await chmod(join(out, executable), 0o755);
const licenses = [['LICENSE','LICENSE'], ['NOTICE','NOTICE'], ['packages/pi-tui/LICENSE','LICENSE.pi-tui'], ['third-party-licenses/jsquash-webp.LICENSE','LICENSE.jsquash-webp'], ['third-party-licenses/libwebp.LICENSE','LICENSE.libwebp']];
const zip = new ZipFile();
zip.addFile(join(out, executable), executable, { mode: 0o100755 });
for (const [src, name] of licenses) { await copyFile(src, join(out, name)); zip.addFile(src, name); }
zip.end();
const archive = resolve('dist-bun', `coderai-bun-${target}.zip`);
await pipeline(zip.outputStream, createWriteStream(archive));
await writeFile(`${archive}.sha256`, `${createHash('sha256').update(await readFile(archive)).digest('hex')}  coderai-bun-${target}.zip\n`);
run(join(out, executable), ['--help']);
