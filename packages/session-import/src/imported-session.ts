import { open, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import { SESSION_META_VERSION } from '@coderai/agent-core-v2/session/sessionMetadata/sessionMetadata';
import { SESSION_INDEX_KEY, withSessionIndexFileLock } from '@coderai/agent-core-v2/app/workspace/workspaceAlias';

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
 * kimi-cli sessions. The generated title kind keeps the imported title when
 * the session is continued. The staging directory is published by the caller.
 */
export async function writeImportedSession(input: ImportedSessionInput, stagingDir: string): Promise<void> {
  const sessionDir = importedSessionDir(input.targetHome, input.workDir, input.sessionId);
  await writeMainAgentWire(stagingDir, {
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
  await writeFile(join(stagingDir, 'state.json'), JSON.stringify(state, null, 2), { mode: 0o600, flag: 'wx' });
}

/** Keep lookup by id working immediately, including after a retried index write. */
export async function ensureSessionIndexEntry(targetHome: string, entry: { sessionId: string; sessionDir: string; workDir: string }): Promise<void> {
  const path = join(targetHome, SESSION_INDEX_KEY);
  await withSessionIndexFileLock(path, async () => {
    let contents: string;
    try { contents = await readFile(path, 'utf8'); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      contents = '';
    }
    for (const line of contents.trimEnd().split(/\r?\n/).reverse()) {
      try {
        const existing = JSON.parse(line);
        if (existing?.sessionId !== entry.sessionId) continue;
        if (existing.sessionDir === entry.sessionDir && existing.workDir === entry.workDir) return;
        break;
      } catch { /* Skip malformed index rows. */ }
    }
    const file = await open(path, 'a', 0o600);
    try {
      const separator = contents && !contents.endsWith('\n') ? '\n' : '';
      await file.writeFile(`${separator}${JSON.stringify(entry)}\n`);
      await file.sync();
    } finally { await file.close(); }
  });
}
