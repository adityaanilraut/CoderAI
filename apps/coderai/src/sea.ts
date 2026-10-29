import { createRequire } from 'node:module';

// Bun does not implement node:sea. Load it only when running under Node.
const sea: typeof import('node:sea') | undefined = process.versions['bun']
  ? undefined
  : createRequire(import.meta.url)('node:sea');

export const isSea = (): boolean => sea?.isSea() ?? false;
export const getAsset = (key: string): ArrayBuffer => {
  if (!sea) throw new Error('Node SEA assets are unavailable in this runtime');
  return sea.getAsset(key);
};
