import { build } from 'esbuild';
import { writeFile, mkdir, chmod, rm, copyFile } from 'node:fs/promises';
import { join } from 'node:path';
import { engineBundlePlugins } from './bundle-options.mjs';

const native = process.argv.includes('--bun-native');
const nativeDir = 'dist-bun';

if (!native) await rm('dist', { recursive: true, force: true });
await mkdir(native ? nativeDir : 'dist', { recursive: true });
const options = { bundle: true, tsconfigRaw: { compilerOptions: { experimentalDecorators: true } }, platform: 'node', target: 'node24', format: 'esm', sourcemap: true, plugins: engineBundlePlugins({ bundleDependencies: native }), logLevel: 'warning', metafile: true, legalComments: native ? 'external' : 'eof' };
if (native) {
  const nativeOptions = { ...options, format: 'cjs', sourcemap: false, define: { 'import.meta.url': '__nativeModuleUrl' }, banner: { js: `var __nativeModuleUrl = require('node:url').pathToFileURL(process.execPath).href;` } };
  const main = await build({ ...nativeOptions, entryPoints: ['apps/coderai/src/main.ts'], outfile: join(nativeDir, 'main.cjs') });
  const worker = await build({ ...options, entryPoints: ['packages/minidb/src/worker/text-build-worker.ts'], outfile: join(nativeDir, 'text-build-worker.mjs') });
  await writeFile(join(nativeDir, 'metafiles.json'), JSON.stringify([main.metafile, worker.metafile]));
  console.error('Built native bundles.');
  process.exit(0);
}
const main = await build({ ...options, entryPoints: ['apps/coderai/src/main.ts'], outfile: 'dist/coderai.mjs', banner: { js: "#!/usr/bin/env bun\nimport { createRequire as __createRequire } from 'node:module';\nimport { fileURLToPath as __fileURLToPath } from 'node:url';\nimport { dirname as __dirnameOf } from 'node:path';\nconst require = __createRequire(import.meta.url);\nconst __filename = __fileURLToPath(import.meta.url);\nconst __dirname = __dirnameOf(__filename);" } });
await build({ ...options, entryPoints: ['packages/minidb/src/worker/text-build-worker.ts'], outfile: 'dist/text-build-worker.mjs' });
await writeFile('dist/metafile.json', JSON.stringify(main.metafile, null, 2));
await chmod('dist/coderai.mjs', 0o755);
await copyFile('LICENSE', 'dist/LICENSE');
await copyFile('NOTICE', 'dist/NOTICE');
await copyFile('packages/pi-tui/LICENSE', 'dist/LICENSE.pi-tui');
await copyFile('third-party-licenses/jsquash-webp.LICENSE', 'dist/LICENSE.jsquash-webp');
await copyFile('third-party-licenses/libwebp.LICENSE', 'dist/LICENSE.libwebp');
console.error('Built dist/coderai.mjs and MiniDb worker; prompt assets are embedded.');
