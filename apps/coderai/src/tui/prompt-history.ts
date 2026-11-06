import { createHash, randomUUID } from 'node:crypto';
import { mkdir, open, realpath, rename, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import lockfile from 'proper-lockfile';

const maxEntries = 100;
const maxPrompt = 32_768;
const maxBytes = 4 * 1024 * 1024;
export function mergePromptHistory(saved: readonly string[], recent: readonly string[]): string[] {
  let overlap = Math.min(saved.length, recent.length);
  while (overlap && !recent.slice(0, overlap).every((text, index) => saved[saved.length - overlap + index] === text)) overlap--;
  return [...saved, ...recent.slice(overlap)].slice(-maxEntries);
}
function decode(text: string): string[] {
  const entries: string[] = [];
  for (const line of text.split('\n')) {
    try {
      const value = JSON.parse(line) as { content?: unknown };
      if (typeof value?.content === 'string' && value.content.trim() && value.content.length <= maxPrompt) entries.push(value.content);
    } catch { /* An interrupted or older entry does not hide the rest of the history. */ }
  }
  return entries.slice(-maxEntries);
}

export class PromptHistory {
  private writes = Promise.resolve();
  constructor(private readonly home: string) {}
  private async file(workspace: string): Promise<string> {
    const canonical = await realpath(workspace).catch(() => resolve(workspace));
    const hash = createHash('sha256').update(canonical).digest('hex');
    return join(this.home, 'input-history', `${hash}.jsonl`);
  }
  private async read(file: string): Promise<string[]> {
    const handle = await open(file, 'r').catch(error => {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
      throw error;
    });
    if (!handle) return [];
    try {
      const size = (await handle.stat()).size;
      const offset = Math.max(0, size - maxBytes);
      const buffer = Buffer.alloc(Math.min(size, maxBytes));
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
      let text = buffer.subarray(0, bytesRead).toString('utf8');
      if (offset) text = text.slice(text.indexOf('\n') + 1);
      return decode(text);
    } finally { await handle.close(); }
  }
  async load(workspace: string): Promise<string[]> {
    await this.writes;
    return this.read(await this.file(workspace));
  }
  append(workspace: string, content: string): Promise<void> {
    const value = content.trim();
    if (!value || value.length > maxPrompt) return Promise.resolve();
    const write = this.writes.then(async () => {
      const file = await this.file(workspace);
      const directory = join(this.home, 'input-history');
      await mkdir(directory, { recursive: true, mode: 0o700 });
      const release = await lockfile.lock(file, { realpath: false, stale: 10_000, retries: { retries: 8, minTimeout: 25, maxTimeout: 100 } });
      const temp = join(directory, `.history-${randomUUID()}`);
      try {
        const entries = await this.read(file);
        if (entries.at(-1) === value) return;
        entries.push(value);
        await writeFile(temp, entries.slice(-maxEntries).map(content => JSON.stringify({ content })).join('\n') + '\n', { mode: 0o600, flag: 'wx' });
        await rename(temp, file);
      } finally {
        try { await rm(temp, { force: true }); }
        finally { await release(); }
      }
    });
    this.writes = write.catch(() => {});
    return write;
  }
  async flush(): Promise<void> { await this.writes; }
}
