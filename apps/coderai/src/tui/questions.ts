import { Key, matchesKey, type Component } from '@coderai/pi-tui';
import type { QuestionRequest } from '@coderai/sdk';
import { ChoiceDialog, PromptDialog } from './dialogs.js';

type Answer = { values: string[]; other: string; picker?: ChoiceDialog; input?: PromptDialog };
/** A batch owns all question state, so navigating never rebuilds prior selections or drafts. */
export class QuestionDialog implements Component {
  private index = 0;
  private editingOther = false;
  private readonly state: Answer[];
  private child!: ChoiceDialog | PromptDialog;
  private _focused = false;
  answers: Record<string, string> | null = null;
  constructor(private readonly request: QuestionRequest, private readonly rows: () => number,
    private readonly finish: (value: string) => void, private readonly refresh: () => void) {
    this.state = request.questions.map(() => ({ values: [], other: '' }));
    this.show();
  }
  get focused(): boolean { return this._focused; }
  set focused(value: boolean) { this._focused = value; if (this.child) this.child.focused = value; }
  invalidate(): void { this.child.invalidate(); }
  render(width: number): string[] { return this.child.render(width); }
  private saveDraft(): void {
    const state = this.state[this.index];
    if (state?.input && this.child === state.input) state.other = state.input.value;
    if (state?.picker && this.child === state.picker && this.request.questions[this.index]?.multiSelect) state.values = state.picker.values;
  }
  private next(): void { this.saveDraft(); if (this.state.length === 1) { this.answers = { [this.request.questions[0]!.question]: this.answer(0) }; this.finish('answered'); return; } this.index++; this.editingOther = false; this.show(); this.refresh(); }
  private show(): void {
    const question = this.request.questions[this.index];
    if (!question) {
      const summary = this.request.questions.map((q, i) => `${q.question}\n${this.answer(i) || '(unanswered)'}`).join('\n\n');
      this.child = new ChoiceDialog(`Review answers\n${summary}`, [{ value: 'submit', label: 'Submit answers' }, { value: 'back', label: 'Back to questions' }], this.rows, value => {
        if (value === 'back') { this.index = Math.max(0, this.index - 1); this.show(); this.refresh(); return; }
        if (value !== 'submit') return;
        const unanswered = this.state.findIndex((_, i) => !this.answer(i));
        if (unanswered >= 0) { this.index = unanswered; this.show(); this.refresh(); return; }
        this.answers = Object.fromEntries(this.request.questions.map((q, i) => [q.question, this.answer(i)]));
        this.finish('answered');
      }, this.refresh, { selectHint: 'Enter submit · Esc/← back · Ctrl+C cancel all' });
    } else {
      const state = this.state[this.index]!;
      const title = `${question.header || 'Question'} · ${this.index + 1}/${this.state.length}\n${question.question}${question.body ? '\n\n' + question.body : ''}`;
      const hint = this.index ? 'Esc/← previous · Ctrl+C cancel all' : 'Esc cancel · Ctrl+C cancel all';
      if (this.editingOther || !question.options.length) {
        state.input ??= new PromptDialog(`${title}\n${question.otherDescription ?? 'Enter your answer.'}`, this.rows, answer => {
          state.other = answer.trim();
          if (state.other) { if (!question.options.length) state.values = ['other']; this.next(); }
        }, this.refresh, state.other);
        this.child = state.input;
      } else {
        state.picker ??= new ChoiceDialog(title, [
          ...question.options.map((o, i) => ({ value: String(i), label: o.label, description: o.description })),
          { value: 'other', label: question.otherLabel ?? 'Other', description: question.otherDescription },
          ...(question.multiSelect ? [{ value: '\0done', label: 'Done', pinned: true, description: 'Continue with selected answers' }] : []),
        ], this.rows, value => {
          if (!value) return;
          state.values = question.multiSelect ? state.picker!.values : [value];
          if (state.values.includes('other')) { this.editingOther = true; this.show(); this.refresh(); }
          else this.next();
        }, this.refresh, { multiple: question.multiSelect, checked: state.values, selectHint: question.multiSelect ? `↑/↓ choose · Space toggle · Done continues · ${hint}` : `↑/↓ choose · Enter select · ${hint}` });
        this.child = state.picker;
      }
    }
    this.child.focused = this._focused;
  }
  private answer(index: number): string {
    const q = this.request.questions[index]!, state = this.state[index]!;
    return state.values.map(value => value === 'other' ? state.other.trim() : q.options[Number(value)]?.label ?? '').filter(Boolean).join(', ');
  }
  handleInput(data: string): void {
    if (matchesKey(data, Key.ctrl('c')) || matchesKey(data, Key.ctrl('d'))) { this.answers = null; this.finish(''); return; }
    if (matchesKey(data, Key.escape) || !this.editingOther && !(this.child instanceof PromptDialog) && matchesKey(data, Key.left)) {
      this.saveDraft();
      if (this.editingOther) this.editingOther = false;
      else if (this.index > 0) this.index--;
      else { this.answers = null; this.finish(''); return; }
      this.show(); this.refresh(); return;
    }
    if (!this.editingOther && !(this.child instanceof PromptDialog) && (matchesKey(data, Key.right) || matchesKey(data, Key.tab))) {
      if (this.index < this.state.length) { this.saveDraft(); this.index++; this.show(); this.refresh(); } return;
    }
    this.child.handleInput(data);
  }
}
