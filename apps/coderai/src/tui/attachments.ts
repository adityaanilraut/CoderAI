import chalk from 'chalk';
import { Image, getCapabilities, getImageDimensions, truncateToWidth, type Component, type ImageDimensions } from '@coderai/pi-tui';
import { inspectAttachment, type StagedAttachment } from '../attachment-info.js';
import { colors } from './theme.js';
import { singleLine } from './status.js';

export class AttachmentPane implements Component {
  private paths: string[] = [];
  private cwd = '';
  private version = 0;
  private items: StagedAttachment[] = [];
  private image?: Image;
  private imageKey = '';
  private dimensionSource?: string;
  private dimensions: ImageDimensions | null = null;
  constructor(private readonly refresh: () => void, private readonly rows: () => number) {}
  get entries(): readonly StagedAttachment[] { return this.items; }
  async setPaths(paths: readonly string[], cwd: string): Promise<void> {
    if (cwd === this.cwd && paths.length === this.paths.length && paths.every((path, index) => path === this.paths[index])) return;
    this.cwd = cwd; this.paths = [...paths]; const version = ++this.version;
    this.items = paths.map(path => ({ path, name: path.split(/[\\/]/).pop() || path, mimeType: 'checking…', kind: 'unknown' }));
    this.image = undefined; this.imageKey = ''; this.dimensionSource = undefined; this.dimensions = null; this.refresh();
    // Inspect sequentially so a large attachment list cannot exhaust file descriptors.
    for (let index = 0; index < paths.length; index++) {
      const item = await inspectAttachment(paths[index]!, cwd);
      if (version !== this.version) return;
      this.items[index] = item; this.refresh();
    }
  }
  dispose(): void { this.version++; this.items = []; this.image = undefined; this.dimensionSource = undefined; this.dimensions = null; }
  invalidate(): void { this.image?.invalidate(); }
  render(width: number): string[] {
    if (!this.items.length || width < 4) return [];
    const available = width - 2;
    const count = this.rows() <= 18 ? 2 : 3;
    const title = `Attachments (${this.items.length}) · Ctrl+T remove`;
    const lines = [chalk.hex(colors.primary)(truncateToWidth(title, available, '…'))];
    this.items.slice(0, count).forEach((item, index) => {
      const suffix = ` [${singleLine(item.mimeType)}]`;
      const name = truncateToWidth(`${index + 1}. ${singleLine(item.name)}`, Math.max(1, available - suffix.length), '…');
      lines.push(truncateToWidth(name + suffix, available, '…'));
    });
    if (this.items.length > count) lines.push(chalk.hex(colors.textMuted)(`+${this.items.length - count} more · /attach list`));
    const item = this.items.find(item => item.kind === 'image' && item.preview) ?? this.items[0]!;
    const caps = getCapabilities();
    if (item.preview !== this.dimensionSource) {
      this.dimensionSource = item.preview;
      this.dimensions = item.preview ? getImageDimensions(item.preview, item.mimeType) : null;
    }
    const dimensions = this.dimensions;
    const inline = !!dimensions && dimensions.widthPx > 0 && dimensions.heightPx > 0 &&
      (caps.images === 'iterm2' || (caps.images === 'kitty' && item.mimeType === 'image/png'));
    if (inline && item.preview) {
      if (item !== this.items[0]) lines.push(chalk.hex(colors.textMuted)(truncateToWidth(`Preview: ${singleLine(item.name)}`, available, '…')));
      const key = `${item.path}:${caps.images}:${width}:${this.rows()}`;
      if (key !== this.imageKey) {
        this.imageKey = key;
        this.image = new Image(item.preview, item.mimeType, { fallbackColor: text => chalk.hex(colors.textMuted)(text) },
          { maxWidthCells: Math.min(32, available), maxHeightCells: this.rows() <= 18 ? 2 : 4, filename: singleLine(item.name) }, dimensions!);
      }
      try { return [...lines.map(line => ' ' + line), ...this.image!.render(width)]; }
      catch { /* Unsupported/corrupt media retains its readable label. */ }
    }
    const fallback = item.notice || (item.kind === 'image' ? 'Image preview unavailable' : item.kind === 'video' ? 'Video · text preview' : 'Preview unavailable');
    lines.push(chalk.hex(colors.textMuted)(truncateToWidth(fallback, available, '…')));
    return lines.map(line => ' ' + line);
  }
}
