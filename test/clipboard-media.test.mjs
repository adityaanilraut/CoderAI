import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'tsx/esm/api';
register();
const { readClipboardMedia } = await import('../apps/coderai/src/clipboard-media.ts');

test('clipboard reads macOS files and images', async () => {
  const file = await readClipboardMedia('darwin', async () => Buffer.from(JSON.stringify({ path: '/tmp/a video.mp4' })));
  assert.deepEqual(file, { kind: 'file', path: '/tmp/a video.mp4' });
  const image = await readClipboardMedia('darwin', async () => Buffer.from(JSON.stringify({ image: 'AQID' })));
  assert.deepEqual(image, { kind: 'image', bytes: Buffer.from([1, 2, 3]) });
  const multiple = await readClipboardMedia('darwin', async () => Buffer.from(JSON.stringify({ paths: ['/tmp/notes.txt', '/tmp/movie.mpeg'] })));
  assert.deepEqual(multiple, { kind: 'file', path: '/tmp/movie.mpeg' });
});

test('clipboard reads Wayland file URIs and falls back to X11 images', async () => {
  const file = await readClipboardMedia('linux', async (_command, args) => Buffer.from(args.includes('--list-types') ? 'text/uri-list\n' : 'file:///tmp/a%20video.mp4\n'));
  assert.deepEqual(file, { kind: 'file', path: '/tmp/a video.mp4' });
  const image = await readClipboardMedia('linux', async (command, args) => command === 'wl-paste' ? undefined : Buffer.from(args.includes('TARGETS') ? 'image/png\n' : 'image bytes'));
  assert.deepEqual(image, { kind: 'image', bytes: Buffer.from('image bytes') });
  assert.equal(await readClipboardMedia('linux', async () => undefined), undefined);
});

test('native clipboard uses file references before Finder thumbnails and falls back to image bytes', async () => {
  const runner = async () => undefined;
  const native = { availableFormats: () => ['public.file-url'], getText: async () => 'file:///tmp/clip.mov', hasImage: () => true, getImageBinary: async () => [9] };
  assert.deepEqual(await readClipboardMedia('darwin', runner, native), { kind: 'file', path: '/tmp/clip.mov' });
  const image = { availableFormats: () => [], hasImage: () => true, getImageBinary: async () => [1, 2, 3] };
  assert.deepEqual(await readClipboardMedia('darwin', runner, image), { kind: 'image', bytes: Buffer.from([1, 2, 3]) });
  native.getText = async () => 'invalid';
  assert.equal(await readClipboardMedia('darwin', runner, native), undefined);
});
