import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = fileURLToPath(new URL('../', import.meta.url));
const packageRoot = join(projectRoot, 'packages');
const manifestAt = root => JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const packages = new Map();
for (const entry of readdirSync(packageRoot, { withFileTypes: true })) {
  const root = join(packageRoot, entry.name);
  if (!entry.isDirectory() || !existsSync(join(root, 'package.json'))) continue;
  const manifest = manifestAt(root);
  packages.set(manifest.name, { root, manifest });
}

function packageAt(directory) {
  for (let root = directory; root; root = dirname(root)) {
    if (existsSync(join(root, 'package.json'))) return { root, manifest: manifestAt(root) };
    if (dirname(root) === root) break;
  }
}

function targetFor(entries, specifier) {
  const defaultTarget = entry => typeof entry === 'string' ? entry : entry?.default;
  if (Object.hasOwn(entries ?? {}, specifier)) return defaultTarget(entries[specifier]);
  const matches = Object.keys(entries ?? {}).filter(pattern => {
    const index = pattern.indexOf('*');
    if (index === -1) return false;
    const prefix = pattern.slice(0, index);
    const suffix = pattern.slice(index + 1);
    return specifier.length >= prefix.length + suffix.length
      && specifier.startsWith(prefix) && specifier.endsWith(suffix);
  });
  matches.sort((a, b) => b.indexOf('*') - a.indexOf('*') || b.length - a.length);
  const pattern = matches[0];
  if (!pattern) return undefined;
  const index = pattern.indexOf('*');
  const suffixLength = pattern.length - index - 1;
  return defaultTarget(entries[pattern])?.replaceAll('*', specifier.slice(index, specifier.length - suffixLength));
}

const workspaceAliases = {
  name: 'workspace-aliases',
  setup(build) {
    build.onResolve({ filter: /^#/ }, args => {
      if (!args.path.startsWith('#/') && !args.path.startsWith('#human/')) return;
      const pkg = packageAt(args.resolveDir);
      const target = targetFor(pkg?.manifest.imports, args.path);
      if (!target) throw new Error(`No package import for ${args.path} in ${args.resolveDir || 'the bundle entry point'}`);
      return build.resolve(resolve(pkg.root, target), { resolveDir: args.resolveDir, kind: args.kind });
    });
    build.onResolve({ filter: /^@coderai\// }, args => {
      const parts = args.path.split('/');
      const name = parts.slice(0, 2).join('/');
      const pkg = packages.get(name);
      if (!pkg) throw new Error(`Excluded workspace package: ${name}`);
      const subpath = parts.length > 2 ? `./${parts.slice(2).join('/')}` : '.';
      const target = targetFor(pkg.manifest.exports, subpath);
      if (!target) throw new Error(`Missing workspace export: ${args.path}`);
      return build.resolve(resolve(pkg.root, target), { resolveDir: args.resolveDir, kind: args.kind });
    });
  },
};

const rawAssets = {
  name: 'raw-assets',
  setup(build) {
    build.onResolve({ filter: /\?raw$/ }, args => ({
      path: resolve(args.resolveDir, args.path.slice(0, -4)),
      namespace: 'raw',
    }));
    build.onLoad({ filter: /.*/, namespace: 'raw' }, async args => ({
      contents: await readFile(args.path, 'utf8'),
      loader: 'text',
    }));
  },
};

const runtimeDependencies = {
  name: 'runtime-dependencies',
  setup(build) {
    build.onResolve({ filter: /^[\w@][^:]*$/ }, args => {
      if (args.path.startsWith('@coderai/') || args.path.startsWith('#')) return;
      return { path: /^ajv\/dist\/(2019|2020)$/.test(args.path) ? args.path + '.js' : args.path, external: true };
    });
  },
};

export function engineBundlePlugins({ bundleDependencies = false } = {}) {
  return bundleDependencies ? [workspaceAliases, rawAssets] : [workspaceAliases, rawAssets, runtimeDependencies];
}
