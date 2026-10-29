import chalk from 'chalk';
import { Input, Key, matchesKey, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from '@coderai/pi-tui';
import type { ApprovalRequest } from '@coderai/sdk';
import { colors } from './theme.js';
import { sanitizeTerminalText } from './sanitize.js';
import { describeToolInput } from './display.js';

const choices = [
  { value: 'yes', label: 'Approve once', detail: 'Only this action' },
  { value: 'always', label: 'Remember approval', detail: 'Matching actions in this session' },
  { value: '/yolo', label: 'Enable YOLO', detail: 'Approve this action; auto-approve routine actions' },
  { value: 'feedback', label: 'Reject with feedback', detail: 'Tell the assistant what to change' },
  { value: 'no', label: 'Reject', detail: 'Do not run this action' },
];

/** A focused approval card. Its preview scrolls independently of the actions. */
export class ApprovalDialog implements Component {
  private selected = 4;
  private scroll = 0;
  private maxScroll = 0;
  private readonly input = new Input();
  private readonly title: string;
  private readonly action: string;
  private readonly preview: string;

  constructor(request: ApprovalRequest, private readonly rows: () => number, private readonly finish: (answer: string) => void, private readonly renderAgain: () => void) {
    this.title = sanitizeTerminalText(`Approve ${request.toolName}?`).replaceAll('\n', ' ');
    this.action = sanitizeTerminalText(request.action).replaceAll('\n', ' ');
    this.preview = sanitizeTerminalText(describeToolInput(request.display)).replaceAll('\t', '   ');
    this.input.onSubmit = answer => {
      const value = answer.trim();
      const index = /^[1-5]$/.test(value) ? Number(value) - 1 : undefined;
      this.finish(index === undefined ? value : choices[index]!.value);
    };
  }

  invalidate(): void { this.input.invalidate(); }

  handleInput(data: string): void {
    if (matchesKey(data, Key.up) || matchesKey(data, Key.down) || matchesKey(data, Key.tab)) {
      this.input.setValue('');
      this.selected = (this.selected + (matchesKey(data, Key.up) ? -1 : 1) + choices.length) % choices.length;
    } else if (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) {
      this.scroll = Math.max(0, Math.min(this.maxScroll, this.scroll + (matchesKey(data, Key.pageUp) ? -3 : 3)));
    } else if (matchesKey(data, Key.enter) && !this.input.getValue()) {
      this.finish(choices[this.selected]!.value);
    } else {
      this.input.handleInput(data);
    }
    this.renderAgain();
  }

  render(width: number): string[] {
    const inner = Math.max(1, width - 4);
    const muted = (text: string) => chalk.hex(colors.textMuted)(text);
    const border = (text: string) => chalk.hex(colors.warning)(text);
    const lines = wrapTextWithAnsi(this.preview, inner);
    const compact = this.rows() < 23;
    const previewHeight = Math.max(1, Math.min(9, this.rows() - (compact ? 14 : 19)));
    this.maxScroll = Math.max(0, lines.length - previewHeight);
    this.scroll = Math.min(this.scroll, this.maxScroll);
    const content = [
      ...(compact ? [chalk.hex(colors.warning).bold(this.title)] : [
        chalk.hex(colors.warning).bold('APPROVAL REQUIRED'), chalk.bold(this.title), muted(this.action), '',
      ]),
      ...lines.slice(this.scroll, this.scroll + previewHeight),
      muted(this.maxScroll ? `Preview ${this.scroll + 1}–${Math.min(lines.length, this.scroll + previewHeight)} of ${lines.length} · PgUp/PgDn scroll` : 'Action preview'),
      ...(compact ? [] : ['']),
      ...choices.map((choice, index) => {
        const label = `${index === this.selected ? '›' : ' '} ${index + 1}  ${choice.label}`;
        const text = inner < 60 ? label : `${label}  ${choice.detail}`;
        return index === this.selected ? chalk.hex(colors.primary).bold(text) : text;
      }),
      ...(compact ? [] : ['']),
      muted('↑/↓ choose · Enter confirm · Esc reject'),
      muted('YOLO: /yolo on|off · --yolo'),
      ...(this.input.getValue() ? this.input.render(Math.max(1, inner - 7)).map(line => `Reply: ${line}`) : [muted('Or type 1–5, yes, always, /yolo, no: feedback')]),
    ];
    const framed = content.map(line => {
      const clipped = truncateToWidth(line, inner, '…');
      return `${border('│')} ${clipped}${' '.repeat(Math.max(0, inner - visibleWidth(clipped)))} ${border('│')}`;
    });
    return [border(`╭${'─'.repeat(Math.max(0, width - 2))}╮`), ...framed, border(`╰${'─'.repeat(Math.max(0, width - 2))}╯`)];
  }
}
