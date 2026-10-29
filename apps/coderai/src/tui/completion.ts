import { readdir } from 'node:fs/promises';
import { dirname, basename, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { CombinedAutocompleteProvider, type AutocompleteSuggestions } from '@coderai/pi-tui';

const runFile = promisify(execFile);

// Complete only the first argument so free-form text is never replaced.
const argumentsByCommand: Record<string, readonly string[]> = {
  plan: ['on', 'off', 'show', 'clear'], swarm: ['on', 'off', 'status'],
  tower: ['on', 'off', 'status'], yolo: ['on', 'off', 'status'],
  permissions: ['manual', 'yolo', 'auto'], theme: ['auto', 'dark', 'light'],
  goal: ['status', 'pause', 'resume', 'cancel', 'next', 'replace'],
  queue: ['clear', 'remove'], attach: ['list', 'clear', 'remove'],
  plugins: ['list', 'reload', 'install', 'enable', 'disable', 'remove'],
  mcp: ['reconnect'], 'add-dir': ['list'], resume: ['latest'],
};
const hints: Record<string, string> = {
  model: '[alias]', effort: '<level>', undo: '[count]', stop: '<task-id>',
  output: '<task-id>', btw: '[question]', steer: '<text>',
  'export-md': '<path>', 'export-debug': '[path]', editor: '<command>',
  attach: '[path|list|clear|remove NUMBER]', goal: '<objective|action>',
};

export function commandCompletion(commands: { name: string; description?: string }[]) {
  return commands.map(command => {
    const choices = argumentsByCommand[command.name];
    return {
      ...command,
      argumentHint: hints[command.name] ?? (choices ? `[${choices.join('|')}]` : undefined),
      ...(choices ? { getArgumentCompletions: (prefix: string) => {
        if (/\s/.test(prefix)) return null;
        const matches = choices.filter(value => value.startsWith(prefix.toLowerCase()));
        if (matches.length === 1 && matches[0] === prefix.toLowerCase()) return null;
        return matches.length ? matches.map(value => ({ value, label: value })) : null;
      } } : {}),
    };
  });
}
export class CompletionProvider extends CombinedAutocompleteProvider {
  constructor(commands: { name: string; description?: string }[], private readonly cwd: string, private readonly additionalDirs: readonly string[] = []) {
    super(commandCompletion(commands), cwd);
  }

  override async getSuggestions(lines: string[], row: number, col: number, options: { signal: AbortSignal; force?: boolean }): Promise<AutocompleteSuggestions | null> {
    const before = (lines[row] ?? '').slice(0, col);
    const match = before.match(/(?:^|\s)(@(?:"[^"\n]*|[^\s"\n]*))$/);
    if (!match?.[1]) return super.getSuggestions(lines, row, col, options);
    const prefix = match[1];
    const quoted = prefix.startsWith('@"');
    const path = prefix.slice(quoted ? 2 : 1);
    const directory = path.endsWith('/') ? path : dirname(path);
    const search = path.endsWith('/') ? '' : basename(path);
    try {
      const entries = await readdir(resolve(this.cwd, directory), { withFileTypes: true });
      if (options.signal.aborted) return null;
      const base = directory === '.' ? '' : directory.replace(/\/$/, '') + '/';
      const items = entries.filter(entry => entry.name !== '.git' && entry.name.toLowerCase().startsWith(search.toLowerCase())).slice(0, 100).map(entry => {
        const name = base + entry.name + (entry.isDirectory() ? '/' : '');
        const value = quoted || name.includes(' ') ? `@"${name}"` : `@${name}`;
        return { value, label: name };
      });
      if (directory !== '.' || !search) return items.length ? { prefix, items } : null;
      const roots = [this.cwd, ...this.additionalDirs];
      const matches = await Promise.all(roots.map(async (root, index) => {
        try {
          const result = await runFile('rg', ['--files', '--hidden', '-g', '!.git', '-g', '!node_modules'], { cwd: root, signal: options.signal, maxBuffer: 4 * 1024 * 1024 });
          const needle = search.toLowerCase();
          return result.stdout.split('\n').filter(name => name.toLowerCase().includes(needle)).slice(0, 100).map(name => {
            const path = index === 0 ? name : resolve(root, name);
            return { value: quoted || path.includes(' ') ? `@"${path}"` : `@${path}`, label: path };
          });
        } catch { return []; }
      }));
      if (options.signal.aborted) return null;
      const combined = [...items, ...matches.flat()].filter((item, index, all) => all.findIndex(other => other.value === item.value) === index).slice(0, 100);
      return combined.length ? { prefix, items: combined } : null;
    } catch { return null; }
  }
}
