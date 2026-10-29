import chalk from 'chalk';
import { Input, Key, matchesKey, truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from '@coderai/pi-tui';
import { colors } from './theme.js';
import { sanitizeTerminalText } from './sanitize.js';

export class TrustDialog implements Component {
  private selected = 1;
  private scroll = 0;
  private maxScroll = 0;
  private readonly input = new Input({ prompt: 'Reply: ' });
  private readonly details: string;

  constructor(workDir: string, private readonly exitOnCancel: boolean, private readonly rows: () => number, private readonly finish: (answer: string) => void, private readonly renderAgain: () => void, disclosure?: string) {
    this.details = `${sanitizeTerminalText(disclosure ?? `Trust ${workDir}?\n\nProject instructions can guide the assistant. Hooks, plugins and configured MCP programs may run on your computer.`)}\n\nTrust is saved for this workspace.`;
    this.input.onSubmit = answer => this.finish(['yes', '1'].includes(answer.trim().toLowerCase()) ? 'yes' : '');
  }

  invalidate(): void { this.input.invalidate(); }

  handleInput(data: string): void {
    if (matchesKey(data, Key.escape)) this.finish('');
    else if (matchesKey(data, Key.up) || matchesKey(data, Key.down) || matchesKey(data, Key.tab)) {
      this.input.setValue('');
      this.selected = 1 - this.selected;
    } else if (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) {
      this.scroll = Math.max(0, Math.min(this.maxScroll, this.scroll + (matchesKey(data, Key.pageUp) ? -3 : 3)));
    } else if (matchesKey(data, Key.enter) && !this.input.getValue()) this.finish(this.selected === 0 ? 'yes' : '');
    else this.input.handleInput(data);
    this.renderAgain();
  }

  render(width: number): string[] {
    const inner = Math.max(1, width - 4);
    const muted = (text: string) => chalk.hex(colors.textMuted)(text);
    const border = (text: string) => chalk.hex(colors.primary)(text);
    const details = wrapTextWithAnsi(this.details, inner);
    const bodyHeight = Math.max(1, Math.min(10, this.rows() - 13));
    this.maxScroll = Math.max(0, details.length - bodyHeight);
    this.scroll = Math.min(this.scroll, this.maxScroll);
    const cancel = this.exitOnCancel ? 'Exit' : 'Cancel';
    const content = [
      chalk.hex(colors.primary).bold('WORKSPACE TRUST'), '',
      ...details.slice(this.scroll, this.scroll + bodyHeight),
      muted(this.maxScroll ? 'PgUp/PgDn · scroll details' : ''), '',
      ...['Trust and continue', `${cancel} without trusting`].map((label, index) => {
        const text = `${this.selected === index ? '›' : ' '} ${index + 1}  ${label}`;
        return this.selected === index ? chalk.hex(colors.primary).bold(text) : text;
      }), '',
      muted(`↑/↓ choose · Enter confirm · Esc ${cancel.toLowerCase()}`),
      ...(this.input.getValue() ? this.input.render(inner) : [muted('Or type yes to trust · 2 to cancel')]),
    ];
    const framed = content.map(line => {
      const clipped = truncateToWidth(line, inner, '…');
      return `${border('│')} ${clipped}${' '.repeat(Math.max(0, inner - visibleWidth(clipped)))} ${border('│')}`;
    });
    return [border(`╭${'─'.repeat(Math.max(0, width - 2))}╮`), ...framed, border(`╰${'─'.repeat(Math.max(0, width - 2))}╯`)];
  }
}
