import { execFile } from 'node:child_process';
import { getNativeClipboard } from '@coderai/pi-tui';
export type ClipboardCopyMethod = 'native' | 'osc52';
/** OSC 52 reaches the user's terminal over SSH; local clipboard access is verified when available. */
export async function copyTextToClipboard(text: string): Promise<ClipboardCopyMethod> {
  const osc52 = !!process.stdout.isTTY;
  if (osc52) process.stdout.write(`\x1b]52;c;${Buffer.from(text).toString('base64')}\x07`);
  try {
    const clipboard = getNativeClipboard();
    if (clipboard?.setText) { await clipboard.setText(text); return 'native'; }
  } catch { /* Try platform commands. */ }
  const commands: [string, string[]][] = process.platform === 'darwin' ? [['pbcopy', []]] : process.platform === 'win32' ? [['clip.exe', []]] : [['wl-copy', []], ['xclip', ['-selection', 'clipboard']]];
  for (const [command, args] of commands) {
    try {
      await new Promise<void>((resolve, reject) => {
        const child = execFile(command, args, { timeout: 2000, maxBuffer: 8192 }, error => error ? reject(error) : resolve());
        child.stdin?.on('error', () => {}); child.stdin?.end(text);
      });
      return 'native';
    } catch { /* OSC 52 is still available in remote terminals. */ }
  }
  if (osc52) return 'osc52';
  throw new Error('No clipboard is available.');
}
