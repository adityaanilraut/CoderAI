// Keep parser state across model deltas so a split escape sequence cannot reach the terminal.
export class TerminalSanitizer {
  private state: 'text' | 'escape' | 'escape-intermediate' | 'csi' | 'string' | 'string-escape' = 'text';

  write(input: string): string {
    let output = '';
    for (const char of input) {
      const code = char.codePointAt(0)!;
      switch (this.state) {
        case 'text':
          if (code === 0x1b) this.state = 'escape';
          else if (code === 0x9b) this.state = 'csi';
          else if ([0x90, 0x98, 0x9d, 0x9e, 0x9f].includes(code)) this.state = 'string';
          else if (code === 0x0a || code === 0x09 || (code >= 0x20 && !(code >= 0x7f && code <= 0x9f))) output += char;
          break;
        case 'escape':
          if (char === '[') this.state = 'csi';
          else if (']PX^_'.includes(char)) this.state = 'string';
          else if (code >= 0x20 && code <= 0x2f) this.state = 'escape-intermediate';
          else this.state = code === 0x1b ? 'escape' : 'text';
          break;
        case 'escape-intermediate':
          if (code === 0x1b) this.state = 'escape';
          else if (code >= 0x30 && code <= 0x7e) this.state = 'text';
          break;
        case 'csi':
          if (code === 0x1b) this.state = 'escape';
          else if (code >= 0x40 && code <= 0x7e) this.state = 'text';
          break;
        case 'string':
          if (code === 0x07 || code === 0x9c) this.state = 'text';
          else if (code === 0x1b) this.state = 'string-escape';
          break;
        case 'string-escape':
          if (char === '\\' || code === 0x07 || code === 0x9c) this.state = 'text';
          else if (code !== 0x1b) this.state = 'string';
          break;
      }
    }
    return output;
  }
}

export function sanitizeTerminalText(input: string): string {
  return new TerminalSanitizer().write(input);
}
