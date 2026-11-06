import { open } from 'node:fs/promises';
import { constants } from 'node:fs';
import { basename, resolve } from 'node:path';
import { detectFileType, MEDIA_SNIFF_BYTES } from '@coderai/sdk/media';

export type StagedAttachment = { path: string; name: string; mimeType: string; kind: string; preview?: string; notice?: string };

/** Inspect without staging/uploading. Keep preview reads bounded, even for videos or special files. */
export async function inspectAttachment(path: string, cwd: string): Promise<StagedAttachment> {
  const item: StagedAttachment = { path, name: basename(path), mimeType: 'unknown', kind: 'unknown' };
  try {
    const file = await open(resolve(cwd, path), constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      const stat = await file.stat();
      if (!stat.isFile() || !stat.size) throw new Error('Not a nonempty regular file');
      const header = Buffer.alloc(Math.min(stat.size, MEDIA_SNIFF_BYTES));
      const { bytesRead } = await file.read(header, 0, header.length, 0);
      const type = detectFileType(path, header.subarray(0, bytesRead), 'media');
      item.mimeType = type.mimeType; item.kind = type.kind;
      if (type.kind === 'image' && stat.size <= 4 * 1024 * 1024 && ['image/png', 'image/jpeg', 'image/gif'].includes(type.mimeType)) {
        const bytes = Buffer.alloc(stat.size);
        let offset = 0;
        while (offset < bytes.length) {
          const read = await file.read(bytes, offset, bytes.length - offset, offset);
          if (!read.bytesRead) break;
          offset += read.bytesRead;
        }
        if (offset === bytes.length) item.preview = bytes.toString('base64');
      }
      if (type.kind !== 'image' && type.kind !== 'video' && type.kind !== 'audio') item.notice = 'Unsupported media';
    } finally { await file.close(); }
  } catch (error) { item.notice = `Unavailable: ${(error as NodeJS.ErrnoException).code || (error as Error).message}`; }
  return item;
}
