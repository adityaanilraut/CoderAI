import { Markdown } from './markdown.js';
import {  Text, truncateToWidth, type Component } from '@coderai/pi-tui';
import type { CoderaiHarness, Event, Session, PromptSkillActivation } from '@coderai/sdk';
import { markdownTheme } from './theme.js';
import { sanitizeTerminalText, TerminalSanitizer } from './sanitize.js';
import { formatHookResult, formatInterruption } from '../event-format.js';

export class BtwPanel implements Component {
  running = false;
  private readonly turns: { prompt: string; answer: string; thinking: string; status: string; sanitizer: TerminalSanitizer }[] = [];
  private offset = 0;
  constructor(private readonly rows: () => number) {}
  submit(prompt: string): void {
    this.turns.push({ prompt: sanitizeTerminalText(prompt), answer: '', thinking: '', status: 'Answering…', sanitizer: new TerminalSanitizer() });
    if (this.turns.length > 20) this.turns.shift();
    this.running = true; this.offset = 0;
  }
  append(text: string, thinking = false): void {
    const turn = this.turns.at(-1); if (!turn) return;
    if (thinking) turn.thinking = (turn.thinking + sanitizeTerminalText(text)).slice(-16_384);
    else turn.answer = (turn.answer + turn.sanitizer.write(text)).slice(-65_536);
  }
  done(status = 'Completed'): void { const turn = this.turns.at(-1); if (turn) turn.status = sanitizeTerminalText(status); this.running = false; }
  scroll(direction: 'up' | 'down'): void { this.offset = Math.max(0, this.offset + (direction === 'up' ? 3 : -3)); }
  invalidate(): void {}
  private renderAnswer(turn: { answer: string; thinking: string; status: string }, width: number): string[] {
    const markdown = new Markdown(turn.answer || (turn.thinking ? `Thinking: ${turn.thinking}` : turn.status), 1, 0, markdownTheme);
    markdown.setTransient(turn === this.turns.at(-1) && this.running);
    return markdown.render(width);
  }
  render(width: number): string[] {
    if (width < 1) return [];
    const body = this.turns.flatMap(turn => [
      ...new Text(`Q: ${turn.prompt}`, 1, 0).render(width),
      ...this.renderAnswer(turn, width),
      ...new Text(turn.status, 1, 0).render(width),
    ]);
    const budget = Math.max(3, Math.floor(this.rows() / 3));
    this.offset = Math.min(this.offset, Math.max(0, body.length - budget));
    const end = body.length - this.offset;
    return [truncateToWidth(' /btw · follow-up below · PgUp/PgDn scroll · Esc close', width, '…'), ...body.slice(Math.max(0, end - budget), end)];
  }
}

/** Uses a child agent in the current session; never forks or changes main-turn busy state. */
export class BtwPanelController {
  private active?: { session: Session; agentId: string; panel: BtwPanel; off: () => void };
  private opening?: Promise<void>;
  private readonly agentIds = new Set<string>();
  private readonly releasing = new Set<Promise<void>>();
  private generation = 0;
  private task?: Promise<void>;
  constructor(private readonly harness: Pick<CoderaiHarness, 'withInteractiveAgent'>,
    private readonly mount: (panel?: BtwPanel) => void, private readonly refresh: () => void, private readonly rows: () => number,
    private readonly error: (error: unknown) => void) {}
  get isActive(): boolean { return this.active !== undefined; }
  get isRunning(): boolean { return this.opening !== undefined || this.active?.panel.running === true; }
  owns(agentId: string): boolean { return this.agentIds.has(agentId); }
  reset(): void { this.close(); }
  async open(session: Session, question: string, skills: readonly PromptSkillActivation[] = []): Promise<void> {
    if (this.opening) return;
    this.close();
    const generation = this.generation;
    const opening = this.start(session, generation, question, skills);
    this.opening = opening;
    try { await opening; }
    finally { if (this.opening === opening) this.opening = undefined; }
  }
  private async start(session: Session, generation: number, question: string, skills: readonly PromptSkillActivation[]): Promise<void> {
    try {
      const agentId = await session.startBtw();
      this.agentIds.add(agentId);
      if (generation !== this.generation || session.isClosed) { this.release(session, agentId); return; }
      const panel = new BtwPanel(this.rows);
      const off = session.onEvent(event => { if (event.agentId === agentId) { this.route(event); } });
      this.active = { session, agentId, panel, off }; this.mount(panel);
      this.send(question, skills);
    } catch (error) { if (generation === this.generation && !session.isClosed) this.error(error); }
  }
  send(question: string, skills: readonly PromptSkillActivation[] = []): boolean {
    const active = this.active;
    if (!active || active.panel.running || !question.trim()) return false;
    active.panel.submit(question); this.refresh();
    this.task = this.harness.withInteractiveAgent(active.agentId, () => skills.length ? active.session.promptWithSkills(question, skills) : active.session.prompt(question)).catch(error => {
      if (this.active !== active) return;
      active.panel.done(`Failed: ${String(error)}`); this.refresh();
    });
    return true;
  }
  private route(event: Event): void {
    const panel = this.active?.panel; if (!panel) return;
    switch (event.type) {
      case 'assistant.delta': panel.append(event.delta); break;
      case 'thinking.delta': panel.append(event.delta, true); break;
      case 'hook.result': panel.append(`\n${formatHookResult(event)}\n`); break;
      case 'turn.step.retrying': panel.append(`Retrying ${event.nextAttempt}/${event.maxAttempts}`, true); break;
      case 'turn.step.interrupted': { const message = formatInterruption(event); if (message) panel.done(message); break; }
      case 'error': panel.done(event.message); break;
      case 'turn.ended': panel.done(event.reason === 'completed' ? 'Completed' : event.error?.message ?? `Side question ${event.reason}`); break;
    }
    this.refresh();
  }
  cancel(): boolean {
    const active = this.active;
    if (!active?.panel.running) return false;
    void this.harness.withInteractiveAgent(active.agentId, () => active.session.cancel()).catch(this.error);
    return true;
  }
  close(): boolean {
    this.generation++;
    const active = this.active; if (!active) return this.opening !== undefined;
    active.off(); this.active = undefined; this.mount(); this.refresh();
    this.release(active.session, active.agentId);
    return true;
  }
  private release(session: Session, agentId: string): void {
    if (session.isClosed) { this.agentIds.delete(agentId); return; }
    const closing = session.closeBtw(agentId).catch(error => { if (!session.isClosed) this.error(error); }).finally(() => {
      this.agentIds.delete(agentId); this.releasing.delete(closing);
    });
    this.releasing.add(closing);
  }
  scroll(direction: 'up' | 'down'): boolean { if (!this.active) return false; this.active.panel.scroll(direction); this.refresh(); return true; }
  async dispose(): Promise<void> {
    this.close();
    await this.opening;
    await Promise.all([this.task, ...this.releasing]);
  }
}
