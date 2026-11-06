import { mkdir, open, unlink, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import type { PromptInput, Session, CoderaiHarness } from '@coderai/sdk';
import { buildImageCompressionCaption, buildMediaPathTag, compressImageForModel, sessionMediaOriginalsDir } from '@coderai/sdk';
import { detectFileType, MEDIA_SNIFF_BYTES } from '@coderai/sdk/media';
import { buildDaemonFileUrl } from '@coderai/sdk/media';
import { persistOriginalImage } from '@coderai/sdk/media';

const MAX_VIDEO_BYTES = 100 * 1024 * 1024;
const STAGING_TTL_SECONDS = 60 * 60;

/** Stage media in the engine file store for session persistence and provider upload. */
export async function attachmentPrompt(harness: CoderaiHarness, session: Session, prompt: string, paths: readonly string[], cwd: string, format: 'parts' | 'references' = 'parts'): Promise<PromptInput> {
  const capabilities = (await session.getStatus()).mediaCapabilities;
  const parts: PromptInput[number][] = [{ type: 'text', text: prompt }];
  const staged: string[] = [];
  const references: string[] = [];
  try {
    for (const path of paths) {
      const file = await open(resolve(cwd, path), constants.O_RDONLY | constants.O_NONBLOCK);
      try {
        const stat = await file.stat();
        if (!stat.isFile()) throw new Error(`Attachment is not a regular file: ${path}`);
        if (!stat.size) throw new Error(`Attachment is empty: ${path}`);
        const header = Buffer.alloc(Math.min(stat.size, MEDIA_SNIFF_BYTES));
        const { bytesRead } = await file.read(header, 0, header.length, 0);
        const type = detectFileType(path, header.subarray(0, bytesRead), 'media');
        if (type.kind !== 'image' && type.kind !== 'video' && type.kind !== 'audio') throw new Error(`Unsupported attachment format: ${path}.`);
        if (!capabilities?.[type.kind]) throw new Error(`The selected model does not support ${type.kind} input: ${path}`);
        if (type.kind !== 'image' && stat.size > MAX_VIDEO_BYTES) throw new Error(`${type.kind === 'audio' ? 'Audio' : 'Video'} exceeds the ${MAX_VIDEO_BYTES / 1024 / 1024} MiB limit: ${path}`);
        const buffer = Buffer.alloc(stat.size + 1);
        let size = 0;
        while (size < buffer.length) {
          const result = await file.read(buffer, size, buffer.length - size, size);
          if (!result.bytesRead) break;
          size += result.bytesRead;
        }
        if (size !== stat.size) throw new Error(`Attachment changed while reading: ${path}`);
        let bytes: Uint8Array = buffer.subarray(0, size);
        let mimeType = type.mimeType;
        if (format === 'references') {
          const sessionDir = session.summary?.sessionDir;
          if (!sessionDir) throw new Error('Cannot store command attachments without a saved session.');
          const dir = join(sessionDir, 'command-media');
          await mkdir(dir, { recursive: true, mode: 0o700 });
          const saved = join(dir, `${randomUUID()}-${basename(path)}`);
          references.push(saved);
          await writeFile(saved, bytes, { flag: 'wx', mode: 0o600 });
          parts.push({ type: 'text', text: buildMediaPathTag(type.kind, saved) });
          continue;
        }
        if (type.kind === 'image') {
          const compressed = await compressImageForModel(bytes, mimeType, { maxEdge: harness.imageLimits?.maxEdgePx() });
          if (compressed.changed) {
            const dir = session.summary?.sessionDir;
            const originalPath = await persistOriginalImage(bytes, mimeType, { dir: dir ? sessionMediaOriginalsDir(dir) : undefined });
            if (originalPath) {
              parts.push({ type: 'text', text: buildImageCompressionCaption({
                original: { width: compressed.originalWidth, height: compressed.originalHeight, byteLength: compressed.originalByteLength, mimeType },
                final: { width: compressed.width, height: compressed.height, byteLength: compressed.finalByteLength, mimeType: compressed.mimeType },
                originalPath,
              }) });
              bytes = compressed.data; mimeType = compressed.mimeType;
            }
          }
        }
        const meta = await harness.uploadFile(bytes, { name: basename(path), mimeType, expiresInSec: STAGING_TTL_SECONDS });
        staged.push(meta.id);
        const url = buildDaemonFileUrl(meta.id);
        if (type.kind === 'image') parts.push({ type: 'image_url', imageUrl: { url } });
        else if (type.kind === 'audio') parts.push({ type: 'audio_url', audioUrl: { url } });
        else parts.push({ type: 'video_url', videoUrl: { url } });
      } finally { await file.close(); }
    }
    return parts;
  } catch (error) {
    await Promise.all(staged.map(id => harness.deleteFile(id).catch(() => {})));
    await Promise.all(references.map(path => unlink(path).catch(() => {})));
    throw error;
  }
}
