import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isAbsolute, join } from 'node:path';
import { createRequire } from 'node:module';
import { readFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isSea, getAsset } from 'node:sea';
import { mediaKindForPath } from '../../../packages/agent-core-v2/src/agent/media/mediaRef.js';

type Result = { kind: 'file'; path: string } | { kind: 'image'; bytes: Buffer };
type Runner = (command: string, args: string[]) => Promise<Buffer | undefined>;
type NativeClipboard = { availableFormats?: () => string[]; getText?: () => Promise<string>; hasImage: () => boolean; getImageBinary: () => Promise<number[]> };

function nativeClipboard(): NativeClipboard | undefined {
  try {
    if (process.platform === 'linux' && !process.env['DISPLAY'] && !process.env['WAYLAND_DISPLAY']) return;
    const require = createRequire(import.meta.url);
    if (!isSea()) return require('@mariozechner/clipboard') as NativeClipboard;
    const dir = mkdtempSync(join(tmpdir(), 'coderai-native-clipboard-'));
    try {
      const path = join(dir, 'clipboard.node');
      writeFileSync(path, Buffer.from(getAsset('clipboard.node')), { mode: 0o700, flag: 'wx' });
      const binding = require(path) as NativeClipboard;
      process.once('exit', () => { try { rmSync(dir, { recursive: true, force: true }); } catch { /* Windows may retain loaded DLLs. */ } });
      return binding;
    } catch (error) { rmSync(dir, { recursive: true, force: true }); throw error; }
  } catch { return undefined; }
}

function isWsl(): boolean {
  if (process.env['WSL_DISTRO_NAME'] || process.env['WSLENV']) return true;
  try { return /microsoft|wsl/i.test(readFileSync('/proc/version', 'utf8')); } catch { return false; }
}

const run: Runner = (command, args) => new Promise(resolve => {
  execFile(command, args, { encoding: 'buffer', timeout: 5000, maxBuffer: 128 * 1024 * 1024 }, (error, stdout) => resolve(error ? undefined : stdout));
});

const macScript = `ObjC.import('AppKit'); ObjC.import('Foundation');
const pb = $.NSPasteboard.generalPasteboard;
const opts = $.NSMutableDictionary.dictionary;
opts.setObjectForKey($.NSNumber.numberWithBool(true), $.NSPasteboardURLReadingFileURLsOnlyKey);
let paths = [];
try { const urls = pb.readObjectsForClassesOptions([$.NSURL], opts); if (urls) for (let i = 0; i < urls.count; i++) paths.push(ObjC.unwrap(urls.objectAtIndex(i).path)); } catch (e) {}
if (!paths.length) { try { const files = ObjC.deepUnwrap(pb.propertyListForType('NSFilenamesPboardType')); if (Array.isArray(files)) paths = files.map(String); } catch (e) {} }
if (!paths.length) { try { const url = ObjC.unwrap(pb.stringForType('public.file-url')); if (url) paths.push(ObjC.unwrap($.NSURL.URLWithString(url).path)); } catch (e) {} }
if (paths.length) JSON.stringify({paths});
else { const image = $.NSImage.alloc.initWithPasteboard(pb); if (!image || !image.TIFFRepresentation) ''; else { const rep = $.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation); const data = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary); JSON.stringify({image: ObjC.unwrap(data.base64EncodedStringWithOptions(0))}); } }`;

function filePath(text: string): string | undefined {
  for (const item of text.split(/[\r\n\0]+/)) {
    const line = item.trim();
    if (!line || line.startsWith('#')) continue;
    try {
      const path = line.startsWith('file://') ? fileURLToPath(line) : line;
      const kind = mediaKindForPath(path);
      if (isAbsolute(path) && (kind === 'image' || kind === 'video')) return path;
    } catch { /* Skip malformed clipboard entries. */ }
  }
  return;
}

/** Read a clipboard file or image on demand, without invoking a shell. */
export async function readClipboardMedia(platform = process.platform, runner: Runner = run, native: NativeClipboard | undefined = runner === run ? nativeClipboard() : undefined): Promise<Result | undefined> {
  if (process.env['TERMUX_VERSION']) return;
  let macImage: Buffer | undefined;
  if (platform === 'darwin') {
    const output = await runner('osascript', ['-l', 'JavaScript', '-e', macScript]);
    try {
      const value = JSON.parse(output?.toString('utf8') || '{}') as { path?: string; paths?: string[]; image?: string };
      const path = filePath((value.paths ?? (value.path ? [value.path] : [])).join('\n'));
      if (path) return { kind: 'file', path };
      if (value.paths?.length || value.path) return;
      if (value.image) macImage = Buffer.from(value.image, 'base64');
    } catch { /* Continue with the native clipboard. */ }
  } else if (platform === 'linux') {
    for (const [command, list, read] of [
      ['wl-paste', ['--list-types'], (type: string) => ['--no-newline', '--type', type]],
      ['xclip', ['-selection', 'clipboard', '-t', 'TARGETS', '-o'], (type: string) => ['-selection', 'clipboard', '-t', type, '-o']],
    ] as const) {
      const types = (await runner(command, [...list]))?.toString('utf8').split(/\r?\n/).map(type => type.trim()) ?? [];
      const uriType = types.find(type => type.toLowerCase().split(';')[0] === 'text/uri-list');
      if (uriType) {
        const path = filePath((await runner(command, read(uriType)))?.toString('utf8') ?? '');
        if (path) return { kind: 'file', path };
      }
      for (const type of ['image/png', 'image/jpeg', 'image/webp', 'image/gif']) {
        const selected = types.find(value => value.toLowerCase().split(';')[0] === type);
        if (!selected && (command !== 'xclip' || types.length)) continue;
        const bytes = await runner(command, read(selected ?? type));
        if (bytes?.length) return { kind: 'image', bytes };
      }
    }
    if (isWsl()) {
      const value = (await runner('powershell.exe', ['-NoProfile', '-NonInteractive', '-STA', '-Command', "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; $i=[System.Windows.Forms.Clipboard]::GetImage(); if ($i) { $s=New-Object System.IO.MemoryStream; $i.Save($s,[System.Drawing.Imaging.ImageFormat]::Png); [Console]::Out.Write([Convert]::ToBase64String($s.ToArray())) }"]))?.toString('utf8').trim();
      if (value) return { kind: 'image', bytes: Buffer.from(value, 'base64') };
    }
  } else if (platform === 'win32') {
    const script = "Add-Type -AssemblyName System.Windows.Forms; if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) { [Console]::Out.WriteLine([System.Windows.Forms.Clipboard]::GetFileDropList()[0]) } elseif ([System.Windows.Forms.Clipboard]::ContainsImage()) { $s=New-Object System.IO.MemoryStream; [System.Windows.Forms.Clipboard]::GetImage().Save($s,[System.Drawing.Imaging.ImageFormat]::Png); [Console]::Out.WriteLine('data:'+[Convert]::ToBase64String($s.ToArray())) }";
    const value = (await runner('powershell.exe', ['-NoProfile', '-NonInteractive', '-STA', '-Command', script]))?.toString('utf8').trim();
    if (value?.startsWith('data:')) return { kind: 'image', bytes: Buffer.from(value.slice(5), 'base64') };
    if (value && isAbsolute(value)) return { kind: 'file', path: value };
  }
  if (native) {
    try {
      const fileLike = native.availableFormats?.().some(format => /file.url|nsfilenames|finder|text\/uri-list/i.test(format)) ?? false;
      if (fileLike && native.getText) {
        const path = filePath(await native.getText());
        if (path) return { kind: 'file', path };
        if (platform === 'darwin') return;
      }
      if (native.hasImage()) {
        const bytes = Buffer.from(await native.getImageBinary());
        if (bytes.length) return { kind: 'image', bytes };
      }
    } catch { /* Continue with platform fallback. */ }
  }
  if (macImage?.length) return { kind: 'image', bytes: macImage };
  return;
}
