import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import type { ImportedSessionMessage } from './imported-session.js';
type NormalizedMessage = ImportedSessionMessage & { readonly toolCalls: readonly unknown[] };
import { buildTurnRecords, splitIntoTurns } from './turn-structure.js';

export const WIRE_PROTOCOL_VERSION = '1.0';

export interface WireWriteInput {
  readonly createdAtMs: number;
  readonly messages: readonly NormalizedMessage[];
  readonly lastUsageTokenCount?: number;
}

export async function writeMainAgentWire(sessionDir: string, input: WireWriteInput): Promise<void> {
  const wireDir = join(sessionDir, 'agents', 'main');
  await mkdir(wireDir, { recursive: true, mode: 0o700 });

  const metadata = {
    type: 'metadata',
    protocol_version: WIRE_PROTOCOL_VERSION,
    created_at: input.createdAtMs,
  };
  const lines: string[] = [JSON.stringify(metadata)];
  // Bare `context.append_message` records alone leave the engine's turn clock
  // at zero on resume: the first live turn would be numbered t0 and collide
  // with the imported history turn the transcript grouping also numbers t0.
  // Interleaving synthesized turn.prompt/turn.ended records advances the clock
  // past the imported turns, so live turns get fresh ids.
  const turns = splitIntoTurns(input.messages);
  const turnRecords = buildTurnRecords(turns, { agentId: 'main', time: input.createdAtMs });
  for (const record of turnRecords) {
    lines.push(JSON.stringify(record));
  }
  if (input.lastUsageTokenCount !== undefined) {
    lines.push(
      JSON.stringify({
        type: 'token_counting.measured',
        agentId: 'main',
        length: input.messages.length,
        tokens: input.lastUsageTokenCount,
        time: input.createdAtMs,
      }),
    );
  }
  await writeFile(join(wireDir, 'wire.jsonl'), lines.join('\n') + '\n', 'utf-8');
}
