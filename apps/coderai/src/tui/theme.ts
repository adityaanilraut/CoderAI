import { loadTuiConfig } from './config.js';
import chalk from 'chalk';
import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { DEFAULT_THEME, highlight, supportsLanguage, type Theme } from 'cli-highlight';
import type { EditorTheme, MarkdownTheme, RgbColor } from '@coderai/pi-tui';

const palettes = {
  dark: { primary: '#61AFEF', textMuted: '#9299A3', success: '#98C379', error: '#E06C75', warning: '#E5C07B' },
  light: { primary: '#005A9C', textMuted: '#595959', success: '#286329', error: '#A22020', warning: '#785000' },
};
export const colors = { ...palettes.dark };
const builtInNames = ['auto', 'dark', 'light'] as const;
export const BACKGROUND_QUERY_TIMEOUT = 200;
let selectedTheme = 'auto';
let themeVersion = 0;
let detectedScheme: 'dark' | 'light' | undefined;

export function backgroundScheme(rgb: RgbColor): 'dark' | 'light' | undefined {
  if (![rgb.r, rgb.g, rgb.b].every(value => Number.isFinite(value) && value >= 0 && value <= 255)) return;
  const [r, g, b] = [rgb.r, rgb.g, rgb.b].map(value => {
    const channel = value / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b! > 0.5 ? 'light' : 'dark';
}

export function environmentScheme(value = process.env['COLORFGBG']): 'dark' | 'light' | undefined {
  const background = value?.split(';').at(-1);
  if (!background || !/^(?:[0-9]|1[0-5])$/.test(background)) return;
  return [0, 1, 2, 3, 4, 5, 6, 8].includes(Number(background)) ? 'dark' : 'light';
}

export async function loadTheme(home: string): Promise<void> {
  const name = (await loadTuiConfig(home)).theme;
  if (!await setTheme(name, home)) await setTheme('dark', home);
}

export function themeCheckpoint(): () => void {
  const name = selectedTheme, palette = { ...colors };
  return () => { selectedTheme = name; themeVersion++; Object.assign(colors, palette); };
}

/** The host owns input and consumes OSC replies; the query never delays startup or explicit themes. */
export async function refreshTerminalTheme(host: {
  queryTerminalBackgroundColor(options: { timeoutMs: number }): Promise<RgbColor | undefined>;
  invalidate(): void;
  requestRender(): void;
}, home: string, active = () => true): Promise<void> {
  if (selectedTheme !== 'auto' || !active()) return;
  const version = themeVersion;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const rgb = await Promise.race([
      host.queryTerminalBackgroundColor({ timeoutMs: BACKGROUND_QUERY_TIMEOUT }),
      new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), BACKGROUND_QUERY_TIMEOUT); }),
    ]);
    if (!active() || version !== themeVersion || !rgb) return;
    const scheme = backgroundScheme(rgb);
    if (!scheme) return;
    detectedScheme = scheme;
    await setTheme('auto', home);
    host.invalidate(); host.requestRender();
  } catch { /* Retain the configured/environment palette on unsupported terminals. */ }
  finally { if (timer) clearTimeout(timer); }
}

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

export async function setTheme(theme: string, home = ''): Promise<boolean> {
  let palette = palettes.dark;
  if (theme === 'light') palette = palettes.light;
  else if (theme === 'auto') {
    palette = palettes[detectedScheme ?? environmentScheme() ?? 'dark'];
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
  selectedTheme = theme; themeVersion++;
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
  highlightCode: (code, language) => {
    const theme: Theme = Object.fromEntries(Object.keys(DEFAULT_THEME).map(key => [key, (text: string) => text]));
    for (const key of ['keyword', 'built_in', 'type', 'title', 'name', 'tag', 'attr', 'section', 'meta'] as const) theme[key] = accent;
    for (const key of ['string', 'regexp', 'addition'] as const) theme[key] = text => chalk.hex(colors.success)(text);
    for (const key of ['number', 'literal', 'symbol'] as const) theme[key] = text => chalk.hex(colors.warning)(text);
    theme.comment = muted; theme.quote = muted; theme.deletion = text => chalk.hex(colors.error)(text);
    const normalized = language?.trim().toLowerCase();
    try { return highlight(code, { language: normalized && supportsLanguage(normalized) ? normalized : 'text', ignoreIllegals: true, theme }).split('\n'); }
    catch { return code.split('\n'); }
  },
};
