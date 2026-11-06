import { randomUUID } from 'node:crypto';
import { mkdir, realpath, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { convertSession, findSessions, importedSessionDir, writeImportedSession, type SessionSource } from '@coderai/session-import';
import type { Runtime } from './runtime.js';
import { identity } from './identity.js';
import { exportMarkdown } from './session-export.js';
import { importBudget, sourceHomes } from './session-import/run-import.js';

/** Open the saved workspace under the same trust and ownership rules as an interactive resume. */
async function openSaved(runtime: Runtime, id: string) {
  runtime.options.resume = id;
  await runtime.trust(async () => false);
  return runtime.open(id);
}

/** Turn indices match the SDK: zero is the first user-visible turn. */
export function parseTurn(value: string): number {
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error('--turn must be a nonnegative integer (zero is the first turn).');
  return Number(value);
}

export function parseLimit(value: string): number {
  if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(Number(value))) throw new Error('--limit must be a positive integer.');
  return Number(value);
}

export async function listSessions(runtime: Runtime, options: { all?: boolean; archived?: boolean; limit?: number; json?: boolean }): Promise<string> {
  const sessions = await runtime.harness.listSessions({ workDir: options.all ? undefined : resolve(runtime.options.cwd), includeArchived: options.archived ? true : undefined });
  const limited = options.limit === undefined ? sessions : sessions.slice(0, options.limit);
  if (options.json) return JSON.stringify(limited, null, 2);
  const clean = (value: string) => value.replaceAll(/[\x00-\x1f\x7f]+/g, ' ').trim();
  return limited.map(s => `${new Date(s.updatedAt).toISOString()}\t${s.id}\t${clean(s.title ?? s.lastPrompt ?? '')}${s.archived ? ' [archived]' : ''}${options.all ? `\t${clean(s.workDir)}` : ''}`).join('\n') || 'No sessions found.';
}

export async function forkSession(runtime: Runtime, id: string, options: { turnIndex?: number; title?: string } = {}): Promise<string> {
  const source = await openSaved(runtime, id);
  const fork = await runtime.harness.forkSession({ id: source.id, ...options });
  try { return fork.id; }
  finally { await fork.close(); }
}

export async function exportSessionMarkdown(runtime: Runtime, id: string, outputPath: string): Promise<string> {
  const source = await openSaved(runtime, id);
  const path = resolve(runtime.options.cwd, outputPath);
  await exportMarkdown(source, path);
  return path;
}

export async function exportSessionZip(runtime: Runtime, id: string, outputPath?: string): Promise<string> {
  const source = await openSaved(runtime, id);
  const result = await runtime.harness.exportSession({ id: source.id, version: identity.version, outputPath: outputPath ? resolve(runtime.options.cwd, outputPath) : undefined });
  return result.zipPath;
}

export async function importSession(runtime: Runtime, source: SessionSource, sourcePath: string): Promise<{ id: string; status: string; issues: readonly string[] }> {
  if (source !== 'codex' && source !== 'claude-code') throw new Error('Import source must be codex or claude-code.');
  runtime.options.resume = undefined;
  await runtime.trust(async () => false);
  const workDir = await realpath(resolve(runtime.options.cwd));
  const id = `ses_${randomUUID()}`;
  const sessionDir = importedSessionDir(runtime.harness.homeDir, workDir, id);
  const attachmentDirectory = join(sessionDir, 'attachments');
  const path = resolve(runtime.options.cwd, sourcePath);
  // Discovery supplies the Codex title database and all descendant rollouts.
  const candidates = await findSessions({ ...sourceHomes(), since: 0 });
  const candidate = candidates.find(item => item.source === source && resolve(item.path) === path);
  const config = await runtime.harness.getConfig();
  const budget = importBudget({ ...config, defaultModel: runtime.options.model ?? config.defaultModel });
  try {
    await mkdir(attachmentDirectory, { recursive: true, mode: 0o700 });
    const result = await convertSession({ source, sourcePath: path, attachmentDirectory, ...budget, subagentPaths: candidate?.subagentPaths, stateDatabasePath: candidate?.stateDatabasePath });
    if (result.status === 'unreliable') throw new Error(`Session import failed (${result.error.code}): ${result.error.message}`);
    const now = Date.now();
    await writeImportedSession({ targetHome: runtime.harness.homeDir, sessionId: id, workDir,
      title: result.title ?? 'Imported session', lastPrompt: result.lastPrompt ?? '', updatedAtMs: Math.round(candidate?.modifiedAt ?? now),
      messages: result.messages, custom: { imported_from: source, imported_source_id: result.sourceId, imported_source_path: path, imported_at: new Date(now).toISOString() } });
    await runtime.harness.listSessions({ sessionId: id }).catch(() => []);
    return { id, status: result.status, issues: result.issues.map(issue => `${issue.code}: ${issue.message}`) };
  } catch (error) {
    await rm(sessionDir, { recursive: true, force: true });
    throw error;
  }
}
