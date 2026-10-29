import { build } from 'esbuild';
import { readFile, writeFile, mkdir, chmod, rm, copyFile } from 'node:fs/promises';
import { resolve, dirname, join } from 'node:path';
import { existsSync, readFileSync } from 'node:fs';
const native = process.argv.includes('--native');
const workspacePackages = new Map();
for (const folder of ['node-sdk','agent-core-v2','klient','kosong','oauth','product','minidb','tree-sitter-bash','pi-tui']) {
  const root = resolve('packages', folder);
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  workspacePackages.set(manifest.name, { root, manifest });
}
const aliases = { name: 'workspace-aliases', setup(b) {
  b.onResolve({ filter: /^#/ }, async args => {
    if (!args.path.startsWith('#/') && !args.path.startsWith('#human/')) return;
    let root = args.resolveDir;
    while (!existsSync(join(root, 'package.json'))) root = dirname(root);
    const mapped = args.path.startsWith('#human/') ? join(resolve('packages/agent-core-v2/src/human'), args.path.slice(7))
      : join(root, root.endsWith('/human') ? '' : 'src', args.path.slice(2));
    return b.resolve(mapped, { resolveDir: args.resolveDir, kind: args.kind });
  });
  b.onResolve({ filter: /^@coderai\// }, async args => {
    const parts = args.path.split('/'); const key = parts.slice(0, 2).join('/');
    const pkg = workspacePackages.get(key); if (!pkg) throw new Error('Excluded workspace package: ' + key);
    const sub = parts.length > 2 ? './' + parts.slice(2).join('/') : '.';
    let entry = pkg.manifest.exports[sub];
    if (!entry) for (const [pattern, value] of Object.entries(pkg.manifest.exports)) {
      if (pattern.includes('*') && sub.startsWith(pattern.split('*')[0])) entry = { default: value.default.replace('*', sub.slice(pattern.split('*')[0].length)) };
    }
    const target = typeof entry === 'string' ? entry : entry?.default;
    if (!target) throw new Error('Missing export: ' + args.path);
    return b.resolve(resolve(pkg.root, target), { resolveDir: args.resolveDir, kind: args.kind });
  });
} };

if (!native) await rm('dist', { recursive: true, force: true });
await mkdir(native ? 'dist-native' : 'dist', { recursive: true });
const external = { name: 'runtime-dependencies', setup(b) {
  b.onResolve({ filter: /^[\w@][^:]*$/ }, args => {
    if (args.path.startsWith('@coderai/') || args.path.startsWith('#')) return;
    return { path: /^ajv\/dist\/(2019|2020)$/.test(args.path) ? args.path + '.js' : args.path, external: true };
  });
} };
const raw = { name: 'raw-assets', setup(b) {
  b.onResolve({ filter: /\?raw$/ }, args => ({ path: resolve(args.resolveDir, args.path.slice(0, -4)), namespace: 'raw' }));
  b.onLoad({ filter: /.*/, namespace: 'raw' }, async args => ({ contents: await readFile(args.path, 'utf8'), loader: 'text' }));
} };
const options = { bundle: true, tsconfigRaw: { compilerOptions: { experimentalDecorators: true } }, platform: 'node', target: 'node24', format: 'esm', sourcemap: true, plugins: [aliases, raw, external], logLevel: 'warning', metafile: true };
if (native) {
  const nativeOptions = { ...options, format: 'cjs', sourcemap: false, plugins: [aliases, raw], define: { 'import.meta.url': '__nativeModuleUrl' }, banner: { js: "const __nativeModuleUrl = require('node:url').pathToFileURL(process.execPath).href;" } };
  await build({ ...nativeOptions, entryPoints: ['apps/coderai/src/main.ts'], outfile: 'dist-native/main.cjs' });
  await build({ ...options, plugins: [aliases, raw], entryPoints: ['packages/minidb/src/worker/text-build-worker.ts'], outfile: 'dist-native/text-build-worker.mjs' });
  console.error('Built native bundles.');
  process.exit(0);
}
const main = await build({ ...options, entryPoints: ['apps/coderai/src/main.ts'], outfile: 'dist/coderai.mjs', banner: { js: "#!/usr/bin/env node\nimport { createRequire as __createRequire } from 'node:module';\nimport { fileURLToPath as __fileURLToPath } from 'node:url';\nimport { dirname as __dirnameOf } from 'node:path';\nconst require = __createRequire(import.meta.url);\nconst __filename = __fileURLToPath(import.meta.url);\nconst __dirname = __dirnameOf(__filename);" } });
await build({ ...options, entryPoints: ['packages/minidb/src/worker/text-build-worker.ts'], outfile: 'dist/text-build-worker.mjs' });
await writeFile('dist/metafile.json', JSON.stringify(main.metafile, null, 2));
await chmod('dist/coderai.mjs', 0o755);
await copyFile('LICENSE', 'dist/LICENSE');
await copyFile('NOTICE', 'dist/NOTICE');
await copyFile('packages/pi-tui/LICENSE', 'dist/LICENSE.pi-tui');
await copyFile('third-party-licenses/jsquash-webp.LICENSE', 'dist/LICENSE.jsquash-webp');
await copyFile('third-party-licenses/libwebp.LICENSE', 'dist/LICENSE.libwebp');
console.error('Built dist/coderai.mjs and MiniDb worker; prompt assets are embedded.');
