import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import { SESSION_META_VERSION } from '@coderai/agent-core-v2/session/sessionMetadata/sessionMetadata';

import { encodeWorkDirKey } from '@coderai/agent-core-v2/_base/utils/workdir-slug';
import { writeMainAgentWire } from './wire-writer.js';

export interface ImportedSessionMessage {
  readonly role: 'user' | 'assistant';
  readonly content: readonly { readonly type: 'text'; readonly text: string }[];
}

export interface ImportedSessionInput {
  readonly targetHome: string;
  readonly sessionId: string;
  readonly workDir: string;
  readonly title: string;
  readonly lastPrompt: string;
  readonly updatedAtMs: number;
  readonly messages: readonly ImportedSessionMessage[];
  readonly custom: Readonly<Record<string, unknown>>;
}

/** Directory of an imported session; conversion attachments belong in its `attachments` child. */
export function importedSessionDir(targetHome: string, workDir: string, sessionId: string): string {
  return join(targetHome, 'sessions', encodeWorkDirKey(workDir), sessionId);
}

/**
 * Write a session converted from another tool as an ordinary CoderAI session.
 *
 * The main agent wire gets the same synthesized turn records as migrated
 * kimi-cli sessions. `state.json` is written last, so a directory without it
 * is an interrupted write the caller can remove and retry. The generated
 * title kind keeps the imported title when the session is continued.
 */
export async function writeImportedSession(input: ImportedSessionInput): Promise<string> {
  const sessionDir = importedSessionDir(input.targetHome, input.workDir, input.sessionId);
  await writeMainAgentWire(sessionDir, {
    createdAtMs: input.updatedAtMs,
    messages: input.messages.map((message) => ({ role: message.role, content: message.content, toolCalls: [] })),
  });
  const state = {
    id: input.sessionId,
    version: SESSION_META_VERSION,
    cwd: input.workDir,
    createdAt: input.updatedAtMs,
    updatedAt: input.updatedAtMs,
    archived: false,
    title: input.title,
    titleKind: 'generated',
    isCustomTitle: false,
    lastPrompt: input.lastPrompt.slice(0, 200),
    lastTurnReason: 'completed',
    agents: { main: { homedir: join(sessionDir, 'agents', 'main'), type: 'main', parentAgentId: null } },
    custom: input.custom,
  };
  await writeFile(join(sessionDir, 'state.json'), JSON.stringify(state, null, 2), 'utf-8');
  await ensureSessionIndexEntry(input.targetHome, { sessionId: input.sessionId, sessionDir, workDir: input.workDir });
  return sessionDir;
}

/** Keep lookup by id working immediately, including after a retried index write. */
export async function ensureSessionIndexEntry(targetHome: string, entry: { sessionId: string; sessionDir: string; workDir: string }): Promise<void> {
  const path = join(targetHome, 'session_index.jsonl');
  let contents: string;
  try { contents = await readFile(path, 'utf8'); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    contents = '';
  }
  for (const line of contents.split(/\r?\n/)) {
    try { if (JSON.parse(line)?.sessionId === entry.sessionId) return; } catch { /* Skip malformed index rows. */ }
  }
  await mkdir(targetHome, { recursive: true, mode: 0o700 });
  await appendFile(path, JSON.stringify(entry) + '\n', 'utf8');
}
