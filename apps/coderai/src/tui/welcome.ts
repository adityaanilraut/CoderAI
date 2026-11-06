import chalk from 'chalk';
import { truncateToWidth, visibleWidth, type Component } from '@coderai/pi-tui';
import { colors } from './theme.js';
import { singleLine } from './status.js';

export function compactPath(path: string, width: number): string {
  const safe = singleLine(path);
  if (visibleWidth(safe) <= width) return safe;
  const parts = safe.split(/[\\/]/);
  let tail = parts.pop() || '';
  while (parts.length && visibleWidth('…/' + parts.at(-1) + '/' + tail) <= width) tail = parts.pop() + '/' + tail;
  return truncateToWidth('…/' + tail, width, '…');
}

export class WelcomePanel implements Component {
  private workspace = '';
  private model = '';
  private ready = false;
  setWorkspace(workspace: string): void { this.workspace = workspace; }
  setModel(model: string, ready = true): void { this.model = singleLine(model); this.ready = ready; }
  invalidate(): void {}
  render(width: number): string[] {
    if (width < 3) return [];
    const inner = width - 2;
    const title = chalk.hex(colors.primary).bold('CoderAI') + chalk.hex(colors.textMuted)(this.ready ? ' · Ready to code' : ' · Welcome');
    const project = 'Project: ' + compactPath(this.workspace, Math.max(1, inner - 9));
    const model = this.ready ? `Model: ${this.model}` : 'Model: not ready · /setup to configure';
    const actions = this.ready ? (inner < 42 ? '/help · /model · /setup' : '/help commands · /model change · /setup') : '/setup connect a model · /help commands';
    return [title, project, chalk.hex(this.ready ? colors.textMuted : colors.warning)(model), chalk.hex(colors.textMuted)(actions)]
      .map(line => ' ' + truncateToWidth(line, inner, '…'));
  }
}
