import type { CoderaiHarness } from '@coderai/sdk';

/** Match upstream's bounded best-effort retries; the engine guards custom titles. */
export class AutoSessionTitle {
  private readonly attempts = new Map<string, number>();
  private readonly pending = new Map<string, Promise<void>>();
  constructor(private readonly harness: Pick<CoderaiHarness, 'getConfig' | 'generateSessionTitle'>,
    private readonly enabled: () => boolean, private readonly updated: (id: string, title: string) => void) {}
  completed(id: string): Promise<void> {
    const current = this.pending.get(id);
    if (current) return current;
    if (!this.enabled() || (this.attempts.get(id) ?? 0) >= 3) return Promise.resolve();
    const task = this.generate(id).finally(() => this.pending.delete(id));
    this.pending.set(id, task); return task;
  }
  private async generate(id: string): Promise<void> {
    try {
      if ((await this.harness.getConfig()).autoSessionTitle === false || !this.enabled()) return;
      this.attempts.set(id, (this.attempts.get(id) ?? 0) + 1);
      const title = await this.harness.generateSessionTitle({ id, source: 'first_turn' });
      if (title !== undefined) { this.attempts.set(id, 3); this.updated(id, title); }
    } catch { /* Best effort: retain the current title. */ }
  }
}
