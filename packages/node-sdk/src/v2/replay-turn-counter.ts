export class ReplayTurnCounter {
  private userTurns = 0;
  private reliable = true;
  private firstPrompt = Infinity;
  private firstUserMessage = Infinity;

  get count(): number | undefined {
    // Older histories can contain user messages that predate turn.prompt records.
    return this.reliable && this.firstUserMessage >= this.firstPrompt
      ? this.userTurns
      : undefined;
  }

  observe(record: unknown, position: number): void {
    if (record === null || typeof record !== 'object') {
      this.invalidate();
      return;
    }
    if (!('type' in record)) return;

    if (record.type === 'turn.prompt') {
      const counted = isUserTurnOrigin('origin' in record ? record.origin : undefined);
      if (counted === true) this.userTurns++;
      if (counted === undefined) this.invalidate();
      this.firstPrompt = Math.min(this.firstPrompt, position);
    } else if (record.type === 'context.append_message' && 'message' in record) {
      const message = record.message;
      if (message === null || typeof message !== 'object' || !('role' in message) || message.role !== 'user') return;
      const origin = 'origin' in message ? message.origin : undefined;
      if (
        origin === undefined || origin === null ||
        (typeof origin === 'object' && 'kind' in origin && origin.kind !== 'shell_command' &&
          isUserTurnOrigin(origin) === true)
      ) {
        this.firstUserMessage = Math.min(this.firstUserMessage, position);
      }
    }
  }

  invalidate(): void {
    this.reliable = false;
  }
}

function isUserTurnOrigin(origin: unknown): boolean | undefined {
  if (origin === null || typeof origin !== 'object' || !('kind' in origin)) return undefined;
  switch (origin.kind) {
    case 'user':
      return true;
    case 'skill_activation':
    case 'plugin_command':
      if (!('trigger' in origin) || typeof origin.trigger !== 'string') return undefined;
      return origin.trigger === 'user-slash';
    case 'shell_command':
      if (!('phase' in origin) || typeof origin.phase !== 'string') return undefined;
      return origin.phase === 'input';
    case 'cron_job':
    case 'cron_missed':
    case 'system_trigger':
    case 'task':
    case 'background_task':
    case 'hook_result':
    case 'compaction_summary':
    case 'injection':
    case 'retry':
      return false;
    default:
      return undefined;
  }
}
