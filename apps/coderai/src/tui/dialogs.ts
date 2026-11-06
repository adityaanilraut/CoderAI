import chalk from 'chalk';
import { Input, Key, matchesKey, fuzzyFilter, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from '@coderai/pi-tui';
import { colors } from './theme.js';
import { sanitizeTerminalText } from './sanitize.js';

export type Choice = { value: string; label: string; description?: string; pinned?: boolean };
const clean = (text: string) => sanitizeTerminalText(text).replace(/\t/g, '   ');
const single = (text: string) => clean(text).replace(/\s+/g, ' ').trim();
function cleanInput(input: Input): void {
  const value = input.getValue();
  const safe = sanitizeTerminalText(value).replace(/[\n\t]/g, ' ');
  if (safe !== value) input.setValue(safe);
}
export const dialogBodyHeight = (rows: number) => Math.max(1, rows - 7);

export function dialogFrame(title: string, body: string[], hints: string[], width: number, rows: number): string[] {
  if (width < 3) return [];
  const inner = Math.max(1, width - 4);
  const border = (text: string) => chalk.hex(colors.primary)(text);
  const content = [chalk.hex(colors.primary).bold(single(title)), ...body.slice(0, dialogBodyHeight(rows)), ...hints.slice(0, 2).map(hint => chalk.hex(colors.textMuted)(hint))];
  return [border('╭' + '─'.repeat(width - 2) + '╮'), ...content.map(line => {
    const clipped = truncateToWidth(line, inner, '…');
    return border('│') + ' ' + clipped + ' '.repeat(Math.max(0, inner - visibleWidth(clipped))) + ' ' + border('│');
  }), border('╰' + '─'.repeat(width - 2) + '╯')].map(line => truncateToWidth(line, width, ''));
}

export class ChoiceDialog implements Component {
  private readonly search = new Input({ prompt: 'Search: ', placeholder: 'Type to filter' });
  private selected = 0;
  private scroll = 0;
  private pageSize = 5;
  private noticePage = 3;
  private noticeMax = 0;
  private readonly checked = new Set<string>();
  private warning = '';
  private readonly title: string;
  private notice: string;
  constructor(title: string, private items: readonly Choice[], private readonly rows: () => number,
    private readonly finish: (answer: string) => void, private readonly refresh: () => void,
    private readonly options: { searchable?: boolean; multiple?: boolean; initial?: string; checked?: readonly string[]; selectHint?: string } = {}) {
    const [heading, ...notice] = clean(title).split('\n'); this.title = heading ?? ''; this.notice = notice.join('\n');
    this.selected = Math.max(0, items.findIndex(item => item.value === options.initial));
    for (const value of options.checked ?? []) this.checked.add(value);
  }
  private filtered(): Choice[] {
    const query = this.search.getValue().toLocaleLowerCase().trim();
    return [...fuzzyFilter(this.items.filter(item => !item.pinned), query, item => `${item.label} ${item.description ?? ''} ${item.value}`), ...this.items.filter(item => item.pinned)];
  }
  get selection(): Choice | undefined { return this.filtered()[this.selected]; }
  get focused(): boolean { return this.search.focused; }
  set focused(value: boolean) { this.search.focused = value; }
  setItems(items: readonly Choice[]): void {
    const previous = this.selection?.value ?? this.options.initial; this.items = items;
    this.selected = Math.max(0, this.filtered().findIndex(item => item.value === previous));
  }
  setNotice(notice: string): void { this.notice = clean(notice); this.scroll = 0; }
  get values(): string[] { return this.items.filter(item => this.checked.has(item.value)).map(item => item.value); }
  invalidate(): void { this.search.invalidate(); }
  handleInput(data: string): void {
    const items = this.filtered();
    if (matchesKey(data, Key.escape)) { this.finish(''); return; }
    if (matchesKey(data, Key.enter) || (this.options.multiple && matchesKey(data, 'space'))) {
      const item = items[this.selected];
      if (!item) return;
      if (this.options.multiple && item.value !== '\0done') {
        this.checked.has(item.value) ? this.checked.delete(item.value) : this.checked.add(item.value); this.warning = '';
      } else if (this.options.multiple && !this.checked.size) this.warning = 'Select at least one answer.';
      else { this.finish(item.value); return; }
    } else if (matchesKey(data, Key.up) || matchesKey(data, Key.down) || matchesKey(data, Key.tab)) {
      this.selected = (this.selected + (matchesKey(data, Key.up) ? -1 : 1) + Math.max(1, items.length)) % Math.max(1, items.length);
    } else if (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) {
      const direction = matchesKey(data, Key.pageUp) ? -1 : 1;
      if (this.noticeMax) this.scroll = Math.max(0, Math.min(this.noticeMax, this.scroll + direction * this.noticePage));
      else this.selected = Math.max(0, Math.min(items.length - 1, this.selected + direction * this.pageSize));
    } else if (matchesKey(data, Key.home)) this.selected = 0;
    else if (matchesKey(data, Key.end)) this.selected = Math.max(0, items.length - 1);
    else if (this.options.searchable) {
      const previous = this.search.getValue(); this.search.handleInput(data); cleanInput(this.search);
      if (previous !== this.search.getValue()) this.selected = 0;
    }
    this.refresh();
  }
  render(width: number): string[] {
    const inner = Math.max(1, width - 4), budget = dialogBodyHeight(this.rows());
    const items = this.filtered(); this.selected = Math.max(0, Math.min(this.selected, items.length - 1));
    const description = this.selection?.description ? wrapTextWithAnsi(clean(this.selection.description), inner).slice(0, budget > 8 ? 2 : 1) : [];
    const notice = this.notice ? wrapTextWithAnsi(this.notice, inner) : [];
    this.noticePage = Math.max(1, Math.min(6, budget - (this.options.searchable ? 1 : 0) - description.length - Math.min(3, items.length) - 2));
    this.noticeMax = Math.max(0, notice.length - this.noticePage); this.scroll = Math.min(this.scroll, this.noticeMax);
    const preview = notice.slice(this.scroll, this.scroll + this.noticePage);
    const preface = [...(this.options.searchable ? this.search.render(inner) : []), ...preview,
      ...(notice.length > this.noticePage ? [chalk.hex(colors.textMuted)(`Preview ${this.scroll + 1}–${this.scroll + preview.length}/${notice.length} · PgUp/PgDn`)] : [])];
    this.pageSize = Math.max(1, Math.min(10, budget - preface.length - description.length - 1));
    const start = Math.max(0, Math.min(this.selected - Math.floor(this.pageSize / 2), items.length - this.pageSize));
    const list = items.slice(start, start + this.pageSize).map((item, index) => {
      const mark = this.options.multiple && item.value !== '\0done' ? `${this.checked.has(item.value) ? '[x]' : '[ ]'} ` : '';
      const text = `${index + start === this.selected ? '›' : ' '} ${mark}${single(item.label)}`;
      return index + start === this.selected ? chalk.hex(colors.primary).bold(text) : text;
    });
    const noMatches = this.search.getValue().trim() && !items.some(item => !item.pinned);
    const count = this.warning || (noMatches ? 'No matches · edit your search' : items.length ? `${this.selected + 1}/${items.length}${this.options.multiple ? ` · ${this.checked.size} selected` : ''}` : 'No matches · edit your search');
    return dialogFrame(this.title, [...preface, ...(list.length ? list : ['No matching options.']), ...description.map(line => chalk.hex(colors.textMuted)(line)), chalk.hex(this.warning ? colors.warning : colors.textMuted)(count)],
      [this.options.selectHint ?? (this.options.multiple ? '↑/↓ choose · Space/Enter toggle · Done submits' : '↑/↓ choose · Enter select · Esc cancel'),
        this.noticeMax ? 'PgUp/PgDn preview · Esc cancel' : this.options.searchable ? 'Type to search · PgUp/PgDn page · Esc cancel' : 'PgUp/PgDn page · Home/End jump · Esc cancel'], width, this.rows());
  }
}

export class PromptDialog implements Component {
  private readonly input = new Input({ prompt: 'Reply: ' });
  private scroll = 0;
  private maxScroll = 0;
  private page = 3;
  constructor(private readonly title: string, private readonly rows: () => number, finish: (answer: string) => void,
    private readonly refresh: () => void, initial = '') {
    this.input.setValue(initial); cleanInput(this.input); this.input.handleInput('\x1b[F'); this.input.onSubmit = finish;
  }
  get value(): string { return this.input.getValue(); }
  get focused(): boolean { return this.input.focused; }
  set focused(value: boolean) { this.input.focused = value; }
  invalidate(): void { this.input.invalidate(); }
  handleInput(data: string): void {
    if (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) this.scroll = Math.max(0, Math.min(this.maxScroll, this.scroll + (matchesKey(data, Key.pageUp) ? -this.page : this.page)));
    else { this.input.handleInput(data); cleanInput(this.input); }
    this.refresh();
  }
  render(width: number): string[] {
    const [title, ...rest] = clean(this.title).split('\n');
    const lines = wrapTextWithAnsi(rest.join('\n'), Math.max(1, width - 4));
    this.page = Math.max(1, dialogBodyHeight(this.rows()) - 2); this.maxScroll = Math.max(0, lines.length - this.page);
    this.scroll = Math.min(this.scroll, this.maxScroll);
    return dialogFrame(title ?? '', [...lines.slice(this.scroll, this.scroll + this.page), ...(this.maxScroll ? [`Question ${this.scroll + 1}–${Math.min(lines.length, this.scroll + this.page)}/${lines.length}`] : []), ...this.input.render(Math.max(1, width - 4))],
      ['Enter submit · Esc cancel', this.maxScroll ? 'PgUp/PgDn scroll question' : 'Type your answer'], width, this.rows());
  }
}

export class DocumentDialog implements Component {
  private scroll = 0;
  private maxScroll = 0;
  private page = 5;
  constructor(private readonly title: string, private readonly content: string, private readonly rows: () => number,
    private readonly finish: (answer: string) => void, private readonly refresh: () => void) {}
  invalidate(): void {}
  handleInput(data: string): void {
    if (matchesKey(data, Key.escape) || matchesKey(data, Key.enter)) { this.finish(''); return; }
    if (matchesKey(data, Key.home)) this.scroll = 0;
    else if (matchesKey(data, Key.end)) this.scroll = this.maxScroll;
    else if (matchesKey(data, Key.up)) this.scroll--;
    else if (matchesKey(data, Key.down)) this.scroll++;
    else if (matchesKey(data, Key.pageUp)) this.scroll -= this.page;
    else if (matchesKey(data, Key.pageDown)) this.scroll += this.page;
    this.scroll = Math.max(0, Math.min(this.maxScroll, this.scroll)); this.refresh();
  }
  render(width: number): string[] {
    const lines = wrapTextWithAnsi(clean(this.content), Math.max(1, width - 4));
    this.page = dialogBodyHeight(this.rows()); this.maxScroll = Math.max(0, lines.length - this.page); this.scroll = Math.min(this.scroll, this.maxScroll);
    const body = lines.slice(this.scroll, this.scroll + this.page).map(line => /^[A-Z][A-Z /&]+$/.test(line) ? chalk.hex(colors.primary).bold(line) : line);
    return dialogFrame(this.title, body, ['↑/↓ scroll · PgUp/PgDn page · Home/End', `Enter/Esc close · ${this.scroll + 1}–${Math.min(lines.length, this.scroll + this.page)}/${lines.length}`], width, this.rows());
  }
}
