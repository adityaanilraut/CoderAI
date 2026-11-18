import { createWriteStream } from 'node:fs';
import { chmod, mkdir, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

import { type Entry, fromBuffer as yauzlFromBuffer } from 'yauzl';

import { Error2, ErrorCodes } from '#/errors';

const MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024;
const MAX_EXTRACTED_BYTES = 512 * 1024 * 1024;
const MAX_ENTRIES = 10_000;

function archiveLimitError(message: string): Error2 {
  return new Error2(ErrorCodes.PLUGIN_LOAD_FAILED, message);
}

function extractedSizeError(): Error2 {
  return archiveLimitError('Extracted plugin exceeds 512 MiB.');
}

export async function downloadZip(url: string, signal?: AbortSignal): Promise<Buffer> {
  const timeout = AbortSignal.timeout(5 * 60 * 1000);
  const resp = await fetch(url, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout });
  if (!resp.ok || resp.body === null) {
    await resp.body?.cancel().catch(() => {});
    throw new Error2(
      ErrorCodes.PLUGIN_LOAD_FAILED,
      `Failed to download zip: HTTP ${resp.status} ${resp.statusText}`,
      { details: { status: resp.status } },
    );
  }
  const reader = resp.body.getReader();
  const chunks: Uint8Array[] = [];
  let downloaded = 0;
  try {
    if (Number(resp.headers.get('content-length')) > MAX_DOWNLOAD_BYTES) {
      throw archiveLimitError('Plugin download exceeds 64 MiB.');
    }
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return Buffer.concat(chunks, downloaded);
      downloaded += value.byteLength;
      if (downloaded > MAX_DOWNLOAD_BYTES) throw archiveLimitError('Plugin download exceeds 64 MiB.');
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export async function extractZip(buffer: Buffer, destDir: string): Promise<string> {
  await mkdir(destDir, { recursive: true });
  const destDirResolved = path.resolve(destDir);
  let settled = false;
  let extracted = 0;
  let declaredBytes = 0;

  await new Promise<void>((resolve, reject) => {
    yauzlFromBuffer(buffer, { lazyEntries: true }, (openErr, zipfile) => {
      if (openErr !== null || zipfile === undefined) {
        reject(
          new Error2(
            ErrorCodes.PLUGIN_LOAD_FAILED,
            `Failed to open zip: ${openErr?.message ?? 'unknown error'}`,
            { cause: openErr ?? undefined },
          ),
        );
        return;
      }

      const rejectArchive = (error: Error): void => {
        if (settled) return;
        settled = true;
        zipfile.close();
        reject(error);
      };
      zipfile.on('error', rejectArchive);
      if (zipfile.entryCount > MAX_ENTRIES) {
        rejectArchive(archiveLimitError('Plugin archive exceeds 10,000 entries.'));
        return;
      }

      const onEntry = (entry: Entry): void => {
        declaredBytes += entry.uncompressedSize;
        if (declaredBytes > MAX_EXTRACTED_BYTES) {
          rejectArchive(extractedSizeError());
          return;
        }
        const fileName = entry.fileName;
        const destPath = path.resolve(destDir, fileName);

        if (destPath !== destDirResolved && !destPath.startsWith(destDirResolved + path.sep)) {
          rejectArchive(new Error2(
            ErrorCodes.PLUGIN_LOAD_FAILED,
            `Path traversal detected in zip entry: ${fileName}`,
            { details: { entry: fileName } },
          ));
          return;
        }

        if (fileName.endsWith('/')) {
          mkdir(destPath, { recursive: true })
            .then(() => zipfile.readEntry())
            .catch(rejectArchive);
          return;
        }

        zipfile.openReadStream(entry, (streamErr, stream) => {
          if (streamErr !== null || stream === undefined) {
            rejectArchive(new Error2(
              ErrorCodes.PLUGIN_LOAD_FAILED,
              `Failed to read ${fileName} from archive: ${streamErr?.message ?? 'unknown error'}`,
              { cause: streamErr ?? undefined, details: { entry: fileName } },
            ));
            return;
          }

          const limit = new Transform({
            transform(chunk: Buffer, _encoding, callback) {
              extracted += chunk.length;
              if (extracted > MAX_EXTRACTED_BYTES) {
                callback(extractedSizeError());
              } else callback(null, chunk);
            },
          });
          mkdir(path.dirname(destPath), { recursive: true })
            .then(() => pipeline(stream, limit, createWriteStream(destPath, { flags: 'wx' })))
            .then(() => restoreFilePermissions(destPath, entry))
            .then(() => zipfile.readEntry())
            .catch(rejectArchive);
        });
      };

      zipfile.on('entry', onEntry);
      zipfile.on('end', () => {
        if (!settled) {
          settled = true;
          resolve();
        }
      });
      zipfile.readEntry();
    });
  });

  return detectPluginRoot(destDir);
}

async function restoreFilePermissions(destPath: string, entry: Entry): Promise<void> {
  const mode = entry.externalFileAttributes >>> 16;
  if (mode === 0) return;
  const permissions = mode & 0o777;
  if (permissions === 0) return;
  await chmod(destPath, permissions);
}

async function detectPluginRoot(dir: string): Promise<string> {
  if (await hasManifest(dir)) return dir;

  const entries = await readdir(dir, { withFileTypes: true });
  const childDirs = entries.filter((entry) => entry.isDirectory());
  const childDir = childDirs.length === 1 ? childDirs[0] : undefined;
  if (childDir !== undefined) {
    const child = path.join(dir, childDir.name);
    if (await hasManifest(child)) return child;
  }

  return dir;
}

async function hasManifest(dir: string): Promise<boolean> {
  const rootManifest = path.join(dir, 'kimi.plugin.json');
  const dirManifest = path.join(dir, '.kimi-plugin', 'plugin.json');
  return (await isFile(rootManifest)) || (await isFile(dirManifest));
}

async function isFile(p: string): Promise<boolean> {
  try {
    return (await stat(p)).isFile();
  } catch {
    return false;
  }
}
