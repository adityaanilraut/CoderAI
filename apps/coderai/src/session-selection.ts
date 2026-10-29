import { realpath } from 'node:fs/promises';
import type { CoderaiHarness } from '@coderai/sdk';

/** Select only sessions belonging to the requested workspace, newest first. */
export async function latestSessionId(harness: CoderaiHarness, cwd: string): Promise<string | undefined> {
  const workDir = await realpath(cwd);
  const sessions = await harness.listSessions({ workDir });
  return [...sessions].sort((a, b) => b.updatedAt - a.updatedAt || b.createdAt - a.createdAt)[0]?.id;
}
