import { randomUUID } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import { resolve } from 'node:path';
import { findSessionDetails, type SessionSource } from '@coderai/session-import';
import { sessionLock, type Runtime } from './runtime.js';
import { identity } from './identity.js';
import { exportMarkdownReplay } from './session-export.js';
import { importBudget, sourceHomes, stageSessionImport } from './session-import/run-import.js';

async function withSavedSession<T>(runtime: Runtime, id: string, operation: () => Promise<T>): Promise<T> {
  const unlock = await sessionLock(runtime.harness.homeDir, id);
  try { return await operation(); }
  finally { await unlock(); }
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
  const query = { workDir: options.all ? undefined : await realpath(resolve(runtime.options.cwd)), includeArchived: options.archived ? true : undefined };
  const sessions = options.limit === undefined
    ? await runtime.harness.listSessions(query)
    : (await runtime.harness.listSessionsPage({ ...query, limit: options.limit })).items;
  if (options.json) return JSON.stringify(sessions, null, 2);
  const clean = (value: string) => value.replaceAll(/[\x00-\x1f\x7f]+/g, ' ').trim();
  return sessions.map(s => `${new Date(s.updatedAt).toISOString()}\t${s.id}\t${clean(s.title ?? s.lastPrompt ?? '')}${s.archived ? ' [archived]' : ''}${options.all ? `\t${clean(s.workDir)}` : ''}`).join('\n') || 'No sessions found.';
}

export async function forkSession(runtime: Runtime, id: string, options: { turnIndex?: number; title?: string } = {}): Promise<string> {
  return withSavedSession(runtime, id, async () => (await runtime.harness.forkSavedSession({ id, ...options })).id);
}

export async function exportSessionMarkdown(runtime: Runtime, id: string, outputPath: string): Promise<string> {
  const path = resolve(runtime.options.cwd, outputPath);
  const { session, agents } = await withSavedSession(runtime, id, () => runtime.harness.readSavedSessionReplay(id));
  await exportMarkdownReplay(session, agents, path);
  return path;
}

export async function exportSessionZip(runtime: Runtime, id: string, outputPath?: string): Promise<string> {
  const result = await withSavedSession(runtime, id, () => runtime.harness.exportSession({
    id, version: identity.version,
    outputDirectory: resolve(runtime.options.cwd),
    outputPath: outputPath ? resolve(runtime.options.cwd, outputPath) : undefined,
  }));
  return result.zipPath;
}

export async function importSession(runtime: Runtime, source: SessionSource, sourcePath: string): Promise<{ id: string; status: string; issues: readonly string[] }> {
  if (source !== 'codex' && source !== 'claude-code') throw new Error('Import source must be codex or claude-code.');
  runtime.options.resume = undefined;
  await runtime.trust(async () => false);
  const workDir = await realpath(resolve(runtime.options.cwd));
  const id = `ses_${randomUUID()}`;
  const path = resolve(runtime.options.cwd, sourcePath);
  const candidate = await findSessionDetails(source, path, sourceHomes().codexHome);
  const config = await runtime.harness.getConfig();
  const budget = importBudget({ ...config, defaultModel: runtime.options.model ?? config.defaultModel });
  const now = Date.now();
  const result = await stageSessionImport({
    coderaiHome: runtime.harness.homeDir, workDir, sessionId: id, source, sourceId: candidate?.sourceId,
    sourcePath: path, now, updatedAtMs: Math.round(candidate?.modifiedAt ?? now), ...budget,
    subagentPaths: candidate?.subagentPaths, stateDatabasePath: candidate?.stateDatabasePath,
  });
  if (result === undefined) return { id, status: 'complete', issues: [] };
  if (result.status === 'unreliable') throw new Error(`Session import failed (${result.error.code}): ${result.error.message}`);
  await runtime.harness.listSessions({ sessionId: id }).catch(() => []);
  return { id, status: result.status, issues: result.issues.map(issue => `${issue.code}: ${issue.message}`) };
}
