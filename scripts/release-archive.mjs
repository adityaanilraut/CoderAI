import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream } from 'node:fs';
import { chmod, readFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { promisify } from 'node:util';

export const nativeArtifactLicenses = [
  ['LICENSE', 'LICENSE'],
  ['NOTICE', 'NOTICE'],
  ['packages/pi-tui/LICENSE', 'LICENSE.pi-tui'],
  ['third-party-licenses/jsquash-webp.LICENSE', 'LICENSE.jsquash-webp'],
  ['third-party-licenses/libwebp.LICENSE', 'LICENSE.libwebp'],
];
export const thirdPartyLicenseFile = 'THIRD_PARTY_LICENSES.txt';

export async function archiveSha256(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

export async function verifyArchiveChecksum(path) {
  const name = basename(path);
  const contents = (await readFile(`${path}.sha256`, 'utf8')).trim();
  const match = /^([a-f0-9]{64}) {2}(\S+)$/.exec(contents);
  if (!match || match[2] !== name) throw new Error(`Invalid checksum file for ${name}`);
  if (await archiveSha256(path) !== match[1]) throw new Error(`Checksum mismatch for ${name}`);
  return match[1];
}

/** Extract the explicitly known flat file layout used by standalone builds. */
export async function unpackNativeArchive(path, destination, executable) {
  const { open } = await import('yauzl');
  const expected = new Set([executable, thirdPartyLicenseFile, ...nativeArtifactLicenses.map(([, name]) => name)]);
  const zip = await promisify(open)(path, { lazyEntries: true, autoClose: false });
  const readStream = promisify(zip.openReadStream.bind(zip));
  try {
    await new Promise((resolve, reject) => {
      zip.on('error', reject);
      zip.on('end', resolve);
      zip.on('entry', entry => {
        if (!expected.delete(entry.fileName)) {
          reject(new Error(`Unexpected or duplicate native archive entry: ${entry.fileName}`));
          return;
        }
        readStream(entry).then(stream =>
          pipeline(stream, createWriteStream(join(destination, entry.fileName), { flags: 'wx' })),
        ).then(() => zip.readEntry(), reject);
      });
      zip.readEntry();
    });
    if (expected.size > 0) throw new Error(`Missing native archive entries: ${[...expected].join(', ')}`);
  } finally {
    zip.close();
  }
  const binary = join(destination, executable);
  if (process.platform !== 'win32') await chmod(binary, 0o755);
  return binary;
}
