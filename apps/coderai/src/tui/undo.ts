export function undoablePrompts<T extends { role: string; origin?: { kind: string } }>(history: readonly T[]): { prompts: T[]; stoppedAtCompaction: boolean } {
  const boundary = history.findLastIndex(message => message.origin?.kind === 'compaction_summary');
  return {
    prompts: history.slice(boundary + 1).filter(message => message.role === 'user' && (!message.origin || message.origin.kind === 'user')),
    stoppedAtCompaction: boundary >= 0,
  };
}

