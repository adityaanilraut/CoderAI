/**
 * Import recent Claude Code and Codex sessions into the CoderAI home.
 *
 * Discovery and conversion live in `@coderai/session-import`; the native session
 * directory writer lives in `@coderai/session-import`. A session id
 * derived from the source id makes reruns skip sessions imported before.
 */
import { createHash } from 'node:crypto';
import { statSync } from 'node:fs';
import { lstat, mkdir, mkdtemp, readdir, readFile, rename, rm } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import lockfile from 'proper-lockfile';

import { effectiveModelAlias, type KimiConfig } from '@coderai/sdk';
import { importedSessionDir, writeImportedSession, ensureSessionIndexEntry } from '@coderai/session-import';
import { convertSession, DEFAULT_IMPORT_RATIO, findSessions, type SessionCandidate, type SessionSource } from '@coderai/session-import';

const DAY_MS = 24 * 60 * 60 * 1000;
const FALLBACK_CONTEXT_WINDOW = 131_072;
const COMPACTION_TRIGGER_RATIO = 0.85;
const RESERVED_CONTEXT_SIZE = 50_000;
const CONTINUATION_MARGIN = 30_000;
const ESTIMATE_UNDERCOUNT = 1.25;
const MIN_IMPORT_BUDGET = 16_384;

export const IMPORT_WINDOW_DAYS = [15, 30] as const;
export const IMPORT_BATCH_LIMIT = 100;

export interface ImportCandidate extends SessionCandidate {
  readonly sessionId: string;
}

export interface ImportScan {
  readonly candidates: readonly ImportCandidate[];
  readonly alreadyImported: number;
  /** Sessions whose project folder no longer exists; they are not candidates. */
  readonly missingFolder: number;
}

export interface ImportBudget {
  readonly contextWindow: number;
  readonly tokenBudget: number;
}

export interface ImportProgress {
  readonly index: number;
  readonly total: number;
  readonly candidate: ImportCandidate;
}

export interface ImportFailure {
  readonly path: string;
  readonly message: string;
}

export interface ImportReport {
  /** Session ids written by this run. */
  readonly imported: string[];
  alreadyImported: number;
  missingFolder: number;
  inUse: number;
  empty: number;
  unsupported: number;
  readonly failed: ImportFailure[];
  readonly warnings: ImportFailure[];
  stopped: boolean;
}

export interface ImportOptions extends ImportBudget {
  readonly coderaiHome: string;
  readonly now: number;
  readonly onProgress?: (progress: ImportProgress) => void;
  readonly shouldStop?: () => boolean;
}

function envPath(env: NodeJS.ProcessEnv, key: string, fallback: string): string {
  const value = env[key]?.trim();
  return value === undefined || value === '' ? fallback : value;
}

export function sourceHomes(
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): { readonly claudeHome: string; readonly codexHome: string } {
  return {
    claudeHome: envPath(env, 'CLAUDE_CONFIG_DIR', join(home, '.claude')),
    codexHome: envPath(env, 'CODEX_HOME', join(home, '.codex')),
  };
}

export function importedSessionId(session: SessionCandidate): string {
  if (/^[\w-]{1,80}$/.test(session.sourceId)) return `ses_${session.sourceId}`;
  return `ses_${createHash('sha256').update(`${session.source}:${session.sourceId}`).digest('hex').slice(0, 32)}`;
}

/**
 * Size imports for the default model, with its `overrides` applied the way
 * the engine applies them. The window is its input limit. The
 * budget keeps the imported message below the engine's automatic compaction
 * threshold (trigger ratio or reserved context, whichever comes first), with
 * room for the system prompt, tools and the first exchange, so continuing an
 * imported session does not start by compacting it away. The import
 * estimator undercounts code and JSON by about a quarter, hence the divisor.
 */
export function importBudget(config: Pick<KimiConfig, 'defaultModel' | 'models' | 'loopControl'>): ImportBudget {
  const alias = config.defaultModel === undefined ? undefined : config.models?.[config.defaultModel];
  const model = alias === undefined ? undefined : effectiveModelAlias(alias);
  const limit = Math.min(model?.maxInputSize ?? Infinity, model?.maxContextSize ?? Infinity);
  const contextWindow = Number.isFinite(limit) ? limit : FALLBACK_CONTEXT_WINDOW;
  const reserved = config.loopControl?.reservedContextSize ?? RESERVED_CONTEXT_SIZE;
  const threshold = Math.min(
    contextWindow * (config.loopControl?.compactionTriggerRatio ?? COMPACTION_TRIGGER_RATIO),
    reserved > 0 && reserved < contextWindow ? contextWindow - reserved : contextWindow,
  );
  const allowance = Math.max(MIN_IMPORT_BUDGET, (threshold - CONTINUATION_MARGIN) / ESTIMATE_UNDERCOUNT);
  return { contextWindow, tokenBudget: Math.floor(Math.min(contextWindow * DEFAULT_IMPORT_RATIO, allowance)) };
}

async function importedState(sessionDir: string): Promise<{ id?: string; custom?: Record<string, unknown> } | undefined> {
  try {
    const state: unknown = JSON.parse(await readFile(join(sessionDir, 'state.json'), 'utf8'));
    if (state === null || typeof state !== 'object') throw new Error('Invalid session metadata');
    return state;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw error;
  }
}

function isDirectory(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

export async function scanImportableSessions(input: {
  readonly coderaiHome: string;
  readonly claudeHome: string;
  readonly codexHome: string;
  readonly now: number;
}): Promise<ImportScan> {
  const since = input.now - Math.max(...IMPORT_WINDOW_DAYS) * DAY_MS;
  const candidates: ImportCandidate[] = [];
  let alreadyImported = 0;
  let missingFolder = 0;
  for (const session of await findSessions({ claudeHome: input.claudeHome, codexHome: input.codexHome, since })) {
    const sessionId = importedSessionId(session);
    const state = await importedState(importedSessionDir(input.coderaiHome, session.cwd, sessionId)).catch(() => undefined);
    if (state?.id === sessionId && state.custom?.['imported_from'] === session.source && state.custom?.['imported_source_id'] === session.sourceId) {
      alreadyImported++;
      await repairIndex(input.coderaiHome, session.cwd, sessionId).catch(() => {});
    } else if (!isDirectory(session.cwd)) missingFolder++;
    else candidates.push({ ...session, sessionId });
  }
  return { candidates, alreadyImported, missingFolder };
}

export function candidatesWithin(scan: ImportScan, days: number, now: number): ImportCandidate[] {
  return scan.candidates.filter((candidate) => candidate.modifiedAt >= now - days * DAY_MS);
}

type Outcome = { warnings: ImportFailure[] } | 'alreadyImported' | 'missingFolder' | 'inUse' | 'empty' | 'unsupported' | ImportFailure;

interface StagedImportInput extends ImportBudget {
  coderaiHome: string;
  workDir: string;
  sessionId: string;
  source: SessionSource;
  sourceId?: string;
  sourcePath: string;
  updatedAtMs: number;
  now: number;
  subagentPaths?: string[];
  stateDatabasePath?: string;
}

async function repairIndex(home: string, workDir: string, sessionId: string): Promise<void> {
  await ensureSessionIndexEntry(home, { sessionId, workDir, sessionDir: importedSessionDir(home, workDir, sessionId) });
}

/** Before staging was introduced, an incomplete import could leave these directories. */
async function isLegacyImportDirectory(sessionDir: string): Promise<boolean> {
  const entries = await readdir(sessionDir, { withFileTypes: true });
  if (!entries.some(entry => entry.name === 'attachments' && entry.isDirectory())) return false;
  if (entries.some(entry => !entry.isDirectory() || !['attachments', 'agents'].includes(entry.name))) return false;
  const attachments = await readdir(join(sessionDir, 'attachments'), { withFileTypes: true });
  if (attachments.some(entry => entry.isDirectory()
    ? !['source', 'images'].includes(entry.name)
    : !entry.isFile() || !/^effective-history-[a-f0-9]{64}\.jsonl$/.test(entry.name))) return false;
  if (entries.some(entry => entry.name === 'agents')) {
    const agents = await readdir(join(sessionDir, 'agents'), { withFileTypes: true });
    if (agents.some(entry => !entry.isDirectory() || entry.name !== 'main')) return false;
    if (agents.length) {
      const main = await readdir(join(sessionDir, 'agents', 'main'), { withFileTypes: true });
      if (main.some(entry => !entry.isFile() || entry.name !== 'wire.jsonl')) return false;
    }
  }
  return true;
}

/** Publish a fully converted session and its attachments under one import lease. */
export async function stageSessionImport(input: StagedImportInput) {
  if (!/^[\w-]+$/.test(input.sessionId)) throw new Error('Invalid import session ID.');
  const sessionDir = importedSessionDir(input.coderaiHome, input.workDir, input.sessionId);
  const stagingRoot = join(input.coderaiHome, 'session-import-staging');
  await mkdir(stagingRoot, { recursive: true, mode: 0o700 });
  let compromised: Error | undefined;
  const release = await lockfile.lock(join(stagingRoot, input.sessionId), {
    realpath: false,
    stale: 30_000,
    retries: { retries: 8, minTimeout: 25, maxTimeout: 100 },
    onCompromised: error => { compromised = error; },
  });
  let stagingDir: string | undefined;
  try {
    const existing = await importedState(sessionDir);
    if (existing !== undefined) {
      if (existing.id !== input.sessionId || existing.custom?.['imported_from'] !== input.source || existing.custom?.['imported_source_id'] !== input.sourceId) {
        throw new Error('Another session already occupies this import ID. Import this file explicitly to create a separate session.');
      }
      await repairIndex(input.coderaiHome, input.workDir, input.sessionId);
      return undefined;
    }
    stagingDir = await mkdtemp(join(stagingRoot, `${input.sessionId}-`));
    const attachmentDirectory = join(stagingDir, 'attachments');
    await mkdir(attachmentDirectory, { mode: 0o700 });
    const result = await convertSession({
      ...input,
      attachmentDirectory,
      publishedAttachmentDirectory: join(sessionDir, 'attachments'),
    });
    if (result.status === 'unreliable') return result;
    await writeImportedSession({
      targetHome: input.coderaiHome,
      sessionId: input.sessionId,
      workDir: input.workDir,
      title: result.title ?? 'Imported session',
      lastPrompt: result.lastPrompt ?? '',
      updatedAtMs: input.updatedAtMs,
      messages: result.messages,
      custom: {
        imported_from: input.source,
        imported_source_id: result.sourceId,
        imported_source_path: input.sourcePath,
        imported_at: new Date(input.now).toISOString(),
      },
    }, stagingDir);
    await mkdir(dirname(sessionDir), { recursive: true, mode: 0o700 });
    const legacy = await lstat(sessionDir).catch(error => {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
      throw error;
    });
    if (legacy !== undefined) {
      if (!legacy.isDirectory() || !await isLegacyImportDirectory(sessionDir) || await importedState(sessionDir) !== undefined) {
        throw new Error('Unrecognized files already occupy the import destination. Import this file explicitly to create a separate session.');
      }
      if (compromised !== undefined) throw compromised;
      await rm(sessionDir, { recursive: true });
    }
    if (compromised !== undefined) throw compromised;
    await rename(stagingDir, sessionDir);
    stagingDir = undefined;
    try {
      await repairIndex(input.coderaiHome, input.workDir, input.sessionId);
    } catch {
      return { ...result, status: 'partial' as const, issues: [...result.issues, {
        stage: 'attachment' as const,
        code: 'SESSION_INDEX_UPDATE_FAILED',
        message: 'The imported session was saved. Its index entry can be repaired on the next scan.',
      }] };
    }
    return result;
  } finally {
    try { if (stagingDir !== undefined) await rm(stagingDir, { recursive: true, force: true }); }
    finally { if (compromised === undefined) await release(); }
  }
}

async function importOne(candidate: ImportCandidate, options: ImportOptions): Promise<Outcome> {
  if (!isDirectory(candidate.cwd)) return 'missingFolder';
  try {
    const result = await stageSessionImport({
      ...options,
      workDir: candidate.cwd,
      sessionId: candidate.sessionId,
      source: candidate.source,
      sourceId: candidate.sourceId,
      sourcePath: candidate.path,
      updatedAtMs: Math.round(candidate.modifiedAt),
      subagentPaths: candidate.subagentPaths,
      stateDatabasePath: candidate.stateDatabasePath,
    });
    if (result === undefined) return 'alreadyImported';
    if (result.status === 'unreliable') {
      if (result.error.code === 'SOURCE_CHANGED') return 'inUse';
      if (result.error.code === 'EMPTY_SOURCE_HISTORY') return 'empty';
      if (result.error.code === 'UNSUPPORTED_HISTORY_BASE') return 'unsupported';
      return { path: candidate.path, message: result.error.message };
    }
    return { warnings: result.issues.map(issue => ({ path: candidate.path, message: `${issue.code}: ${issue.message}` })) };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ELOCKED') return 'inUse';
    return { path: candidate.path, message: error instanceof Error ? error.message : String(error) };
  }
}

/** Import the newest `IMPORT_BATCH_LIMIT` candidates one at a time; `shouldStop` is checked between sessions. */
export async function importSessions(
  candidates: readonly ImportCandidate[],
  options: ImportOptions,
): Promise<ImportReport> {
  const report: ImportReport = { imported: [], alreadyImported: 0, missingFolder: 0, inUse: 0, empty: 0, unsupported: 0, failed: [], warnings: [], stopped: false };
  const batch = candidates.slice(0, IMPORT_BATCH_LIMIT);
  for (const [index, candidate] of batch.entries()) {
    if (options.shouldStop?.() === true) {
      report.stopped = true;
      break;
    }
    options.onProgress?.({ index, total: batch.length, candidate });
    const outcome = await importOne(candidate, options);
    if (typeof outcome === 'string') report[outcome]++;
    else if ('warnings' in outcome) {
      report.imported.push(candidate.sessionId);
      report.warnings.push(...outcome.warnings);
    }
    else report.failed.push(outcome);
  }
  return report;
}
