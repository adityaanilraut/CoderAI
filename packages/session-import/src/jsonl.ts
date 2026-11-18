import { createReadStream } from 'node:fs';
import { createZstdDecompress } from 'node:zlib';

export const MAX_IMPORT_BYTES = 512 * 1024 * 1024;
const MAX_RECORD_BYTES = 32 * 1024 * 1024;

export interface ImportReadBudget {
  remaining: number;
}

export function importSizeError(message: string): Error {
  return Object.assign(new Error(message), { code: 'IMPORT_SIZE_LIMIT' });
}

export async function* readSourceChunks(
  path: string, decompress = path.endsWith('.zst'), budget: ImportReadBudget = { remaining: MAX_IMPORT_BYTES },
): AsyncGenerator<Buffer> {
  const decompressor = decompress ? createZstdDecompress() : undefined;
  const file = createReadStream(path);
  const stream = decompressor ? file.pipe(decompressor) : file;
  if (decompressor) file.on('error', error => decompressor.destroy(error));
  try {
    for await (const chunk of stream as AsyncIterable<Buffer>) {
      budget.remaining -= chunk.length;
      if (budget.remaining < 0) throw importSizeError('The import exceeds the 512 MiB decoded input limit.');
      yield chunk;
    }
  } finally {
    file.destroy();
    decompressor?.destroy();
  }
}

/** Yield byte-exact lines for both parsing and the source archive. */
export async function* readJsonLineBuffers(path: string, budget?: ImportReadBudget): AsyncGenerator<Buffer> {
  let fragments: Buffer[] = [];
  let length = 0;
  const push = (fragment: Buffer): void => {
    length += fragment.length;
    if (length > MAX_RECORD_BYTES + 2) throw recordLimitError();
    fragments.push(fragment);
  };
  const complete = (): Buffer => {
    const line = fragments.length === 1 ? fragments[0]! : Buffer.concat(fragments, length);
    if (recordContentEnd(line) > MAX_RECORD_BYTES) throw recordLimitError();
    return line;
  };
  for await (const chunk of readSourceChunks(path, undefined, budget)) {
    let start = 0;
    for (let end = chunk.indexOf(10); end >= 0; end = chunk.indexOf(10, start)) {
      push(chunk.subarray(start, end + 1));
      yield complete();
      length = 0;
      fragments = [];
      start = end + 1;
    }
    if (start < chunk.length) push(chunk.subarray(start));
  }
  if (length > 0) yield complete();
}

function recordContentEnd(line: Buffer): number {
  let end = line.length;
  if (line[end - 1] === 10) end--;
  if (line[end - 1] === 13) end--;
  return end;
}

function recordLimitError(): Error {
  return importSizeError('An import record exceeds the 32 MiB size limit.');
}

export async function* readJsonLines(path: string): AsyncGenerator<string> {
  for await (const line of readJsonLineBuffers(path)) {
    yield line.toString('utf8', 0, recordContentEnd(line));
  }
}
