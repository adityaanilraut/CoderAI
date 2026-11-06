import { sanitizeTerminalText } from './sanitize.js';
import { homedir } from 'node:os';
import { readdir } from 'node:fs/promises';
import { dirname, basename, resolve, sep } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { CombinedAutocompleteProvider, fuzzyFilter, type AutocompleteItem, type AutocompleteSuggestions } from '@coderai/pi-tui';

const runFile = promisify(execFile);

// Complete only the first argument so free-form text is never replaced.
const argumentsByCommand: Record<string, readonly string[]> = {
  plan: ['on', 'off', 'show', 'clear'], swarm: ['on', 'off', 'status'],
  tower: ['on', 'off', 'status', 'teardown'], yolo: ['on', 'off', 'status'],
  permissions: ['manual', 'yolo', 'auto'], theme: ['auto', 'dark', 'light'],
  goal: ['status', 'pause', 'resume', 'cancel', 'next', 'replace'],
  queue: ['clear', 'remove'], attach: ['list', 'clear', 'remove'],
  plugins: ['list', 'info', 'reload', 'install', 'enable', 'disable', 'remove', 'mcp', 'capabilities'],
  cron: ['list', 'delete'], permission: ['manual', 'yolo', 'auto'], 'auto-title': ['on', 'off'],
  provider: ['refresh'],
  mcp: ['add', 'remove', 'test', 'auth', 'logout', 'reconnect'], 'add-dir': ['list'], resume: ['latest'],
};
const hints: Record<string, string> = {
  'secondary-model': '[alias]', 'subagent-model': '[alias]', model: '[alias]', effort: '<level>', undo: '[count]', stop: '<task-id>',
  output: '<task-id>', btw: '[question]', steer: '<text>',
  'export-md': '<path>', 'export-debug': '[path]', editor: '<command>',
  attach: '[path|list|clear|remove NUMBER]', goal: '<objective|action>',
};

export function commandCompletion(commands: { name: string; description?: string }[]) {
  return commands.map(command => {
    const choices = argumentsByCommand[command.name];
    return {
      ...command, description: command.description ? sanitizeTerminalText(command.description) : undefined,
      argumentHint: hints[command.name] ?? (choices ? `[${choices.join('|')}]` : undefined),
      ...(choices ? { getArgumentCompletions: (prefix: string) => {
        const nested = command.name === 'goal' ? prefix.match(/^(next)\s+(\S*)$/) : command.name === 'plugins' ? prefix.match(/^(mcp|capabilities)\s+(\S*)$/) : null;
        if (nested) {
          const options = nested[1] === 'next' ? ['list', 'manage', 'remove', 'up', 'down'] : nested[1] === 'mcp' ? ['enable', 'disable'] : ['list', 'info', 'install'];
          const matches = options.filter(value => value.startsWith((nested[2] ?? '').toLowerCase()));
          if (matches.length === 1 && matches[0] === (nested[2] ?? '').toLowerCase()) return null;
          return matches.length ? matches.map(value => ({ value: `${nested[1]} ${value}`, label: value })) : null;
        }
        if (/\s/.test(prefix)) return null;
        const matches = choices.filter(value => value.startsWith(prefix.toLowerCase()));
        if (matches.length === 1 && matches[0] === prefix.toLowerCase()) return null;
        return matches.length ? matches.map(value => ({ value, label: value })) : null;
      } } : {}),
    };
  });
}
export class CompletionProvider extends CombinedAutocompleteProvider {
  constructor(private readonly entries: { name: string; description?: string; skillName?: string }[], private readonly cwd: string, private readonly additionalDirs: readonly string[] = [], private readonly suggestFiles?: (root: string, query: string) => Promise<import('@coderai/sdk').SuggestFilesResult | undefined>) {
    super(commandCompletion(entries), cwd);
  }

  override async getSuggestions(lines: string[], row: number, col: number, options: { signal: AbortSignal; force?: boolean }): Promise<AutocompleteSuggestions | null> {
    if (options.signal.aborted) return null;
    const before = (lines[row] ?? '').slice(0, col);
    const match = before.match(/(?:^|[\s=' ])(@(?:"[^"\n]*|[^\s"\n]*))$/);
    if (!match?.[1]) {
      const token = before.match(/(?:^|\s)(\/(?:skill:[^\s/]*|[^\s/]*))$/)?.[1];
      const midPrompt = row > 0 || (token !== undefined && before.length > token.length);
      if (token && midPrompt && !/^\/add-dir\s/.test(before) && !options.force && !lines[0]?.trimStart().startsWith('!')) {
        const skills = this.entries.filter(entry => entry.skillName || entry.name.startsWith('skill:'));
        const items = fuzzyFilter(skills, token.slice(1), item => `skill:${item.skillName ?? item.name.slice(6)}`)
          .map(item => ({ value: `skill:${item.skillName ?? item.name.slice(6)}`, label: `skill:${item.skillName ?? item.name.slice(6)}`, description: item.description ? sanitizeTerminalText(item.description) : undefined, data: { inlineSkill: true } }));
        return items.length ? { prefix: token, items } : null;
      }
      // /add-dir completes directories, including absolute paths, without
      // replacing any free-form arguments of other commands.
      const directory = before.match(/^\/add-dir\s+(.*)$/);
      if (directory && /^(?:\.{1,2}(?:[\\/]|$)|[\\/]|[A-Za-z]:[\\/]|~|")/.test(directory[1] ?? '')) {
        const prefix = directory[1] ?? '';
        const unquoted = prefix.replace(/^"/, '');
        const input = ['~', '.', '..'].includes(unquoted) ? unquoted + '/' : unquoted;
        const separator = Math.max(input.lastIndexOf('/'), input.lastIndexOf(sep));
        const parent = input.slice(0, separator + 1);
        const needle = input.slice(separator + 1);
        const directoryPath = parent.startsWith('~/') ? resolve(homedir(), parent.slice(2)) : resolve(this.cwd, parent);
        try {
          const entries = await readdir(directoryPath, { withFileTypes: true });
          if (options.signal.aborted) return null;
          const items = fuzzyFilter(entries.filter(e => e.isDirectory() && !e.name.startsWith('.') && !/[\x00-\x1f\x7f-\x9f]/.test(e.name)), needle, e => e.name).map(e => {
            const path = parent + e.name + '/';
            return { value: prefix.startsWith('"') || path.includes(' ') ? `"${path}"` : path, label: e.name + '/', data: { directoryArgument: true } };
          });
          return items.length ? { prefix, items } : null;
        } catch { return null; }
      }
      return super.getSuggestions(lines, row, col, options);
    }
    const prefix = match[1];
    const quoted = prefix.startsWith('@"');
    const path = prefix.slice(quoted ? 2 : 1);
    const directory = path.endsWith('/') ? path : dirname(path);
    const search = path.endsWith('/') ? '' : basename(path);
    if (this.suggestFiles) {
      const roots = [this.cwd, ...this.additionalDirs];
      const results = await Promise.all(roots.map(async (root, index) => {
        try {
          const result = await this.suggestFiles!(root, path);
          if (result === undefined) return undefined;
          return result.items.filter(item => !/[\x00-\x1f\x7f-\x9f]/.test(item.path)).map(item => {
            const name = (index === 0 ? item.path : resolve(root, item.path)) + (item.kind === 'directory' && !item.path.endsWith('/') ? '/' : '');
            return { value: quoted || /\s/.test(name) ? `@"${name}"` : `@${name}`, label: name };
          });
        } catch { return undefined; }
      }));
      if (options.signal.aborted) return null;
      if (results.every(result => result !== undefined)) {
        const items = [...new Map(results.flat().filter((item): item is { value: string; label: string } => item !== undefined).map(item => [item.value, item])).values()].slice(0, 100);
        return items.length ? { prefix, items } : null;
      }
    }
    try {
      const entries = await readdir(resolve(this.cwd, directory), { withFileTypes: true });
      if (options.signal.aborted) return null;
      const base = directory === '.' ? '' : directory.replace(/\/$/, '') + '/';
      const items = fuzzyFilter(entries.filter(entry => entry.name !== '.git' && !/[\x00-\x1f\x7f-\x9f]/.test(entry.name)), search, entry => entry.name).slice(0, 100).map(entry => {
        const name = base + entry.name + (entry.isDirectory() ? '/' : '');
        const value = quoted || name.includes(' ') ? `@"${name}"` : `@${name}`;
        return { value, label: name };
      });
      if (directory !== '.' || !search) return items.length ? { prefix, items } : null;
      const roots = [this.cwd, ...this.additionalDirs];
      const matches = await Promise.all(roots.map(async (root, index) => {
        try {
          const result = await runFile('rg', ['--files', '--hidden', '-g', '!.git', '-g', '!node_modules'], { cwd: root, signal: options.signal, maxBuffer: 4 * 1024 * 1024 });
          return fuzzyFilter(result.stdout.split('\n').filter(Boolean), path, name => name).slice(0, 100).map(name => {
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
  override applyCompletion(lines: string[], row: number, col: number, item: AutocompleteItem, prefix: string) {
    const line = lines[row] ?? '';
    if (item.data?.['inlineSkill'] || item.data?.['directoryArgument']) {
      const value = item.data?.['inlineSkill'] ? `/${item.value} ` : item.value;
      const quoted = item.data?.['directoryArgument'] && value.endsWith('"');
      const end = quoted && line[col] === '"' ? col + 1 : col;
      const result = [...lines]; result[row] = line.slice(0, col - prefix.length) + value + line.slice(end);
      return { lines: result, cursorLine: row, cursorCol: col - prefix.length + value.length - (quoted ? 1 : 0) };
    }
    if (prefix.startsWith('@')) {
      const live = line.slice(0, col).match(/(?:^|[\s='" ])(@(?:"[^"\n]*|[^\s"\n]*))$/)?.[1];
      if (!live) return { lines, cursorLine: row, cursorCol: col };
      return super.applyCompletion(lines, row, col, item, live);
    }
    return super.applyCompletion(lines, row, col, item, prefix);
  }

}
