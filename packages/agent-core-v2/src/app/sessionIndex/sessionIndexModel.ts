import type { SessionSummary } from './sessionIndex';
import type { IQueryStore } from '#/persistence/interface/queryStore';

export const SESSION_INDEX_MANIFEST = 'sessionIndex';

export const SESSION_INDEX_SCHEMA_VERSION = 3;

export const PARENT_INDEX_NAME = 'byParent';

export interface SessionWorkspaceCounts {
  readonly active: number;
  readonly archived: number;
}

const writes = new WeakMap<IQueryStore, Promise<void>>();

/** Keep counter read/modify/write operations serialized with row updates. */
export function serializeSessionIndexWrite<T>(store: IQueryStore, operation: () => Promise<T>): Promise<T> {
  const result = (writes.get(store) ?? Promise.resolve()).then(operation);
  writes.set(store, result.then(() => undefined, () => undefined));
  return result;
}

export function sessionCollection(generation: number): string {
  return `session:g${generation}`;
}

export function sessionCountersCollection(generation: number): string {
  return `sessionCounters:g${generation}`;
}

export function recencyColumn(generation: number): string {
  return `g${generation}:updatedAt`;
}

export function withRecencyField(generation: number, summary: SessionSummary): SessionSummary {
  return { ...summary, [recencyColumn(generation)]: summary.updatedAt };
}

export function stripRecencyField(generation: number, record: SessionSummary): SessionSummary {
  const key = recencyColumn(generation);
  if (!(key in record)) return record;
  const rest: Record<string, unknown> = { ...record };
  delete rest[key];
  return rest as unknown as SessionSummary;
}
