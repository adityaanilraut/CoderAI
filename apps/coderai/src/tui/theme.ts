import chalk from 'chalk';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { highlight } from 'cli-highlight';
import type { EditorTheme, MarkdownTheme } from '@coderai/pi-tui';

const palettes = {
  dark: { primary: '#61AFEF', textMuted: '#9299A3', success: '#98C379', error: '#E06C75', warning: '#E5C07B' },
  light: { primary: '#005A9C', textMuted: '#595959', success: '#286329', error: '#A22020', warning: '#785000' },
};
export const colors = { ...palettes.dark };
const builtInNames = ['auto', 'dark', 'light'] as const;

export async function listThemes(home: string): Promise<string[]> {
  try {
    const entries = await readdir(join(home, 'themes'), { withFileTypes: true });
    return entries.filter(entry => entry.isFile() && entry.name.endsWith('.json'))
      .map(entry => entry.name.slice(0, -5)).filter(name => validCustomName(name))
      .sort((a, b) => a.localeCompare(b));
  } catch { return []; }
}

function validCustomName(name: string): boolean {
  return name.length > 0 && name !== '.' && name !== '..' && !name.includes('/') && !name.includes('\\') && !builtInNames.some(value => value === name);
}

export async function setTheme(theme: string, home: string): Promise<boolean> {
  let palette = palettes.dark;
  if (theme === 'light') palette = palettes.light;
  else if (theme === 'auto') {
    const background = process.env['COLORFGBG']?.split(';').at(-1);
    if (background && /^(?:[0-9]|1[0-5])$/.test(background) && ![0, 1, 2, 3, 4, 5, 6, 8].includes(Number(background))) palette = palettes.light;
  } else if (theme !== 'dark') {
    if (!validCustomName(theme)) return false;
    try {
      const data: unknown = JSON.parse(await readFile(join(home, 'themes', `${theme}.json`), 'utf8'));
      if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
      const definition = data as Record<string, unknown>;
      if (typeof definition['name'] !== 'string' || !definition['name'] || (definition['base'] !== undefined && definition['base'] !== 'dark' && definition['base'] !== 'light') || (definition['colors'] !== undefined && (!definition['colors'] || typeof definition['colors'] !== 'object' || Array.isArray(definition['colors'])))) return false;
      palette = { ...palettes[definition['base'] === 'light' ? 'light' : 'dark'] };
      for (const [key, value] of Object.entries((definition['colors'] ?? {}) as Record<string, unknown>)) {
        if (Object.hasOwn(palette, key) && typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value)) palette[key as keyof typeof palette] = value;
      }
    } catch { return false; }
  }
  Object.assign(colors, palette);
  return true;
}
const accent = (s: string) => chalk.hex(colors.primary)(s);
const muted = (s: string) => chalk.hex(colors.textMuted)(s);
export const editorTheme: EditorTheme = {
  borderColor: accent,
  selectList: { selectedPrefix: accent, selectedText: accent, description: muted, scrollInfo: muted, noMatch: muted },
};
export const markdownTheme: MarkdownTheme = {
  heading: s => accent(chalk.bold(s)), link: accent, linkUrl: muted, code: s => chalk.hex(colors.warning)(s),
  codeBlock: s => s, codeBlockBorder: muted, quote: muted, quoteBorder: muted, hr: muted, listBullet: accent,
  bold: s => chalk.bold(s), italic: s => chalk.italic(s), strikethrough: s => chalk.strikethrough(s), underline: s => chalk.underline(s),
  highlightCode: (code, language) => highlight(code, { language, ignoreIllegals: true }).split('\n'),
};
