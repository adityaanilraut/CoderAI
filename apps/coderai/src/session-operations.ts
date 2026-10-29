import { mkdir, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { convertSession, type SessionSource } from '../../../packages/session-import/src/index.js';
import type { Runtime } from './runtime.js';
import { exportMarkdown } from './session-export.js';

/** Open the saved workspace under the same trust and ownership rules as an interactive resume. */
async function openSaved(runtime: Runtime, id: string) {
  runtime.options.resume = id;
  await runtime.trust(async () => false);
  return runtime.open(id);
}

export async function forkSession(runtime: Runtime, id: string): Promise<string> {
  const source = await openSaved(runtime, id);
  const fork = await runtime.harness.forkSession({ id: source.id });
  try { return fork.id; }
  finally { await fork.close(); }
}

export async function exportSessionMarkdown(runtime: Runtime, id: string, outputPath: string): Promise<string> {
  const source = await openSaved(runtime, id);
  const path = resolve(outputPath);
  await exportMarkdown(source, path);
  return path;
}

export async function importSession(runtime: Runtime, source: SessionSource, sourcePath: string): Promise<{ id: string; status: string; issues: readonly string[] }> {
  if (source !== 'codex' && source !== 'claude-code') throw new Error('Import source must be codex or claude-code.');
  runtime.options.resume = undefined;
  await runtime.trust(async () => false);
  const session = await runtime.open('');
  const attachmentDirectory = join(runtime.harness.homeDir, 'imports', session.id);
  await mkdir(join(runtime.harness.homeDir, 'imports'), { recursive: true, mode: 0o700 });
  await mkdir(attachmentDirectory, { mode: 0o700 });
  const status = await session.getStatus();
  const result = await convertSession({ source, sourcePath: resolve(sourcePath), attachmentDirectory, contextWindow: status.maxContextTokens || 128_000 });
  if (result.status === 'unreliable') {
    await runtime.harness.deleteSession(session.id);
    await rm(attachmentDirectory, { recursive: true, force: true });
    throw new Error(`Session import failed (${result.error.code}): ${result.error.message}`);
  }
  const content = result.messages.flatMap(message => message.content.map(part => part.text)).join('\n');
  await session.importContext(content, source);
  if (result.title) await runtime.harness.renameSession({ id: session.id, title: result.title });
  return { id: session.id, status: result.status, issues: result.issues.map(issue => `${issue.code}: ${issue.message}`) };
}
