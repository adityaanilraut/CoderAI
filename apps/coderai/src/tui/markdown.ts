import { Container, Marked, Markdown as BaseMarkdown, Spacer, truncateToWidth, type MarkdownTheme, type TuiMouseEvent, type TuiMouseDispatchResult } from '@coderai/pi-tui';
import { MermaidBlock } from './mermaid-block.js';
import { sanitizeTerminalText } from './sanitize.js';
let renderLatex = true;
let mermaid: 'off' | 'final' = 'final';
let version = 0;
export function setRenderLatex(enabled: boolean): void { if (renderLatex !== enabled) { renderLatex = enabled; version++; } }
export function setMermaidMode(mode: 'off' | 'final'): void { if (mermaid !== mode) { mermaid = mode; version++; } }
const parser = new Marked();
/** Upstream's block segmentation preserves prose spacing and falls back to source for undrawn diagrams. */
export class Markdown extends Container {
  private source: string;
  private seen = -1;
  private transient = false;
  constructor(text: string, private readonly x: number, private readonly y: number, private readonly palette: MarkdownTheme) {
    super(); this.source = sanitizeTerminalText(text);
  }
  setText(text: string): void { this.source = sanitizeTerminalText(text); this.seen = -1; }
  setTransient(value: boolean): void { if (this.transient !== value) { this.transient = value; this.seen = -1; } }
  override invalidate(): void { this.seen = -1; super.invalidate(); }
  override render(width: number): string[] {
    if (!this.source.trim() || width < 1) return [];
    if (this.seen !== version) {
      this.clear(); this.seen = version;
      const prose = (text: string) => new BaseMarkdown(text, this.x, 0, this.palette, undefined, { renderLatex });
      if (mermaid === 'off' || this.transient) this.addChild(prose(this.source));
      else {
        let text = '', previousSpace = true, spacedBefore = true, count = 0;
        const addProse = () => { if (text) { if (count++ && !spacedBefore) this.addChild(new Spacer(1)); this.addChild(prose(text)); text = ''; } };
        for (const token of parser.lexer(this.source)) {
          if (token.type === 'code' && token.lang?.trim().split(/\s+/)[0]?.toLowerCase() === 'mermaid') {
            addProse();
            if (count++ && !previousSpace) this.addChild(new Spacer(1));
            this.addChild(new MermaidBlock(token.text, token.raw, this.x, this.palette, { renderLatex, copySource: true }));
            previousSpace = false;
          } else {
            if (!text) spacedBefore = previousSpace || token.type === 'space';
            text += token.raw; previousSpace = token.type === 'space';
          }
        }
        addProse();
      }
    }
    const lines = super.render(width).map(line => truncateToWidth(line, width, ''));
    return this.y ? [...Array<string>(this.y).fill(' '.repeat(width)), ...lines, ...Array<string>(this.y).fill(' '.repeat(width))] : lines;
  }
  override handleMouse(event: TuiMouseEvent): TuiMouseDispatchResult | undefined {
    return super.handleMouse({ ...event, y: event.y - this.y, height: event.height - 2 * this.y });
  }
}
