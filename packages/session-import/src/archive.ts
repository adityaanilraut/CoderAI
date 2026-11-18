import { createHash } from 'node:crypto';
import { readdir } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { isRequestLogLine } from '#/claude-code';
import { writeAttachmentFile } from '#/images';
import { MAX_IMPORT_BYTES, readJsonLineBuffers, readSourceChunks, type ImportReadBudget } from '#/jsonl';
import type { ConversionIssue, EffectiveHistory, SessionSource, SourceCopy } from '#/types';

export async function saveEffectiveHistory(
  history: EffectiveHistory,
  directory: string,
  issues: readonly ConversionIssue[],
): Promise<string> {
  function* lines(): Generator<string> {
    const { turns, ...header } = history;
    yield JSON.stringify({ type: 'session', format: 'kimi-import-history', version: 1, ...header, issues }) + '\n';
    for (const turn of turns) yield JSON.stringify({ type: 'turn', ...turn }) + '\n';
  }
  const hash = createHash('sha256');
  for (const line of lines()) hash.update(line);
  const filename = `effective-history-${hash.digest('hex')}.jsonl`;
  async function* chunks(): AsyncGenerator<Uint8Array> {
    for (const line of lines()) yield Buffer.from(line);
  }
  return (await writeAttachmentFile(directory, filename, chunks())).path;
}

async function* keptLines(path: string, keep: (line: Buffer) => boolean, budget: ImportReadBudget): AsyncGenerator<Uint8Array> {
  for await (const line of readJsonLineBuffers(path, budget)) if (keep(line)) yield line;
}

async function copyFolder(source: string, destination: string, keep: (line: Buffer) => boolean, budget: ImportReadBudget): Promise<number> {
  let copied = 0;
  for (const entry of await readdir(source, { withFileTypes: true }).catch(() => [])) {
    const path = join(source, entry.name);
    if (entry.isDirectory()) copied += await copyFolder(path, join(destination, entry.name), keep, budget);
    else if (entry.isFile()) {
      const contents = entry.name.endsWith('.jsonl') ? keptLines(path, keep, budget) : readSourceChunks(path, false, budget);
      await writeAttachmentFile(destination, entry.name, contents);
      copied++;
    }
  }
  return copied;
}

export async function saveSourceCopy(source: SessionSource, sourcePath: string, attachmentDirectory: string, subagentPaths: readonly string[] = []): Promise<SourceCopy> {
  const directory = join(attachmentDirectory, 'source');
  const budget = { remaining: MAX_IMPORT_BYTES };
  const keep = source === 'claude-code' ? (line: Buffer) => !isRequestLogLine(line) : () => true;
  await writeAttachmentFile(directory, basename(sourcePath).replace(/\.zst$/, ''), keptLines(sourcePath, keep, budget));
  const session = basename(sourcePath, '.jsonl');
  const folder = source === 'claude-code' && await copyFolder(join(dirname(sourcePath), session), join(directory, session), keep, budget) > 0;
  for (const path of subagentPaths) await writeAttachmentFile(join(directory, 'subagents'), basename(path).replace(/\.zst$/, ''), keptLines(path, keep, budget));
  return { directory, folder, subagentRollouts: subagentPaths.length };
}
