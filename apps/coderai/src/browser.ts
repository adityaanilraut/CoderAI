import { execFile } from 'node:child_process';

export function browserUrl(value: string): string {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || /[\x00-\x20\x7f]/.test(value)) {
    throw new Error('Authorization requires an HTTP(S) URL without embedded credentials or control characters.');
  }
  return url.href;
}

export async function openBrowser(value: string): Promise<boolean> {
  const url = browserUrl(value);
  const [command, args] = process.platform === 'darwin' ? ['open', [url]] as const
    : process.platform === 'win32' ? ['rundll32.exe', ['url.dll,FileProtocolHandler', url]] as const
    : ['xdg-open', [url]] as const;
  return new Promise(resolve => {
    execFile(command, [...args], { timeout: 5000, windowsHide: true }, error => resolve(!error));
  });
}
