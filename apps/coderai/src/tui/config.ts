import { readFile, mkdir, writeFile, rename, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { parse, stringify } from 'smol-toml';
import { z } from 'zod';

export const STATUS_LINE_ITEMS = ['mode', 'goal', 'model', 'tasks', 'cwd', 'git', 'tips'] as const;
const fileSchema = z.object({
  theme: z.string().optional(), tui_mode: z.string().optional(),
  render_latex: z.boolean().optional(), disable_paste_burst: z.boolean().optional(), auto_session_title: z.boolean().optional(),
  editor: z.object({ command: z.string().optional() }).passthrough().optional(),
  notifications: z.object({ enabled: z.boolean().optional(), notification_condition: z.enum(['unfocused', 'always']).optional() }).passthrough().optional(),
  markdown: z.object({ mermaid: z.string().optional() }).passthrough().optional(),
  status_line: z.object({ items: z.array(z.string()).optional(), command: z.string().optional() }).passthrough().optional(),
}).passthrough();
export type TuiConfigPatch = z.infer<typeof fileSchema>;
export interface TuiConfig {
  theme: string; tuiMode: 'regular' | 'fullscreen'; renderLatex: boolean; disablePasteBurst: boolean; autoSessionTitle: boolean;
  editorCommand: string; notifications: { enabled: boolean; condition: 'unfocused' | 'always' };
  markdown: { mermaid: 'off' | 'final' }; statusLine: { items: (typeof STATUS_LINE_ITEMS)[number][] | null; command: string };
}
export const DEFAULT_TUI_CONFIG: TuiConfig = {
  theme: 'auto', tuiMode: 'regular', renderLatex: true, disablePasteBurst: false, autoSessionTitle: true,
  editorCommand: '', notifications: { enabled: true, condition: 'unfocused' }, markdown: { mermaid: 'final' }, statusLine: { items: null, command: '' },
};
async function readRaw(home: string): Promise<TuiConfigPatch> {
  try { return fileSchema.parse(parse(await readFile(join(home, 'tui.toml'), 'utf8'))); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return {}; throw new Error(`Invalid TUI config in ${join(home, 'tui.toml')}: ${error instanceof Error ? error.message : String(error)}`); }
}
export async function loadTuiConfig(home: string, warn: (message: string) => void = () => {}): Promise<TuiConfig> {
  const raw = await readRaw(home);
  // Read the previous theme file only as a migration fallback. All new preferences live here.
  if (raw.theme === undefined) {
    try { raw.theme = (await readFile(join(home, 'theme'), 'utf8')).trim(); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  if (raw.tui_mode !== undefined && !['regular', 'fullscreen'].includes(raw.tui_mode)) warn(`Ignoring unknown tui_mode: ${raw.tui_mode}`);
  if (raw.markdown?.mermaid !== undefined && !['off', 'final'].includes(raw.markdown.mermaid)) warn(`Ignoring unknown markdown.mermaid: ${raw.markdown.mermaid}`);
  const items = raw.status_line?.items?.filter((item): item is (typeof STATUS_LINE_ITEMS)[number] => {
    const known = (STATUS_LINE_ITEMS as readonly string[]).includes(item); if (!known) warn(`Ignoring unknown status_line item: ${item}`); return known;
  });
  return {
    theme: raw.theme || 'auto', tuiMode: raw.tui_mode === 'fullscreen' ? 'fullscreen' : 'regular',
    renderLatex: raw.render_latex ?? true, disablePasteBurst: raw.disable_paste_burst ?? false, autoSessionTitle: raw.auto_session_title ?? true,
    editorCommand: raw.editor?.command?.trim() ?? '',
    notifications: { enabled: raw.notifications?.enabled ?? true, condition: raw.notifications?.notification_condition ?? 'unfocused' },
    markdown: { mermaid: raw.markdown?.mermaid === 'off' ? 'off' : 'final' },
    statusLine: { items: items ? [...new Set(items)] : null, command: raw.status_line?.command?.trim() ?? '' },
  };
}
const pending = new Map<string, Promise<void>>();
/** Serialize read/merge/atomic-replace, preserving unknown preferences and Phase 3 settings. */
export async function saveTuiConfig(home: string, patch: TuiConfigPatch): Promise<void> {
  const task = (pending.get(home) ?? Promise.resolve()).catch(() => {}).then(async () => {
    const raw = await readRaw(home);
    const merged = { ...raw, ...patch };
    for (const key of ['editor', 'notifications', 'markdown', 'status_line'] as const) {
      if (patch[key]) merged[key] = { ...raw[key], ...patch[key] } as never;
    }
    fileSchema.parse(merged);
    await mkdir(home, { recursive: true, mode: 0o700 });
    const temp = join(home, `.tui-${randomUUID()}.toml`);
    try { await writeFile(temp, stringify(merged), { mode: 0o600, flag: 'wx' }); await rename(temp, join(home, 'tui.toml')); }
    finally { await unlink(temp).catch(() => {}); }
  });
  pending.set(home, task);
  try { await task; } finally { if (pending.get(home) === task) pending.delete(home); }
}
export async function saveAutoSessionTitle(home: string, enabled: boolean): Promise<void> { await saveTuiConfig(home, { auto_session_title: enabled }); }
