import { test } from 'node:test';
import assert from 'node:assert/strict';
import { open, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';
import { Jimp } from 'jimp';

test('headless attachments validate type, size and model capability', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64');
  await writeFile(join(f.cwd, 'pixel.png'), png);
  await writeFile(join(f.cwd, 'not-image.png'), 'not an image');
  await writeFile(join(f.cwd, 'video.mp4'), Buffer.from([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]));
  await writeFile(join(f.cwd, 'video.mov'), Buffer.from([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x71, 0x74, 0x20, 0x20]));

  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  const image = await f.run(['--trust', '-p', 'Describe this image', '--attach', 'pixel.png']);
  assert.equal(image.code, 0, image.stderr);
  assert.match(JSON.stringify(f.requests.at(-1)), /data:image\/png;base64,/);
  const bad = await f.run(['-p', 'Look', '--attach', 'not-image.png']);
  assert.equal(bad.code, 1); assert.match(bad.stderr, /Unsupported attachment format/);
  const video = await f.run(['-p', 'Look', '--attach', 'video.mp4']);
  assert.equal(video.code, 1); assert.match(video.stderr, /does not support video input/);
  const mov = await f.run(['-p', 'Look', '--attach', 'video.mov']);
  assert.equal(mov.code, 1); assert.match(mov.stderr, /does not support video input/);
  f.env.CODERAI_MODEL_CAPABILITIES = 'thinking';
  const noImage = await f.run(['-p', 'Look', '--attach', 'pixel.png']);
  assert.equal(noImage.code, 1); assert.match(noImage.stderr, /does not support image input/);
});

test('large image is compressed while its original remains available in the session', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  const image = new Jimp({ width: 2200, height: 1200, color: 0xffcc33ff });
  await writeFile(join(f.cwd, 'large.png'), await image.getBuffer('image/png'));
  const result = await f.run(['--trust', '-p', 'Inspect image', '--attach', 'large.png']);
  assert.equal(result.code, 0, result.stderr);
  const request = JSON.stringify(f.requests.at(-1));
  assert.match(request, /Image compressed to fit model limits/);
  assert.match(request, /media-originals/);
  assert.match(request, /data:image\//);
});

test('image attachments over the shared media limit are rejected before provider dispatch', async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  const path = join(f.cwd, 'oversized.png');
  const image = await open(path, 'w');
  try {
    await image.write(Buffer.from('89504e470d0a1a0a', 'hex'));
    await image.truncate(100 * 1024 * 1024 + 1);
  } finally { await image.close(); }
  const result = await f.run(['--trust', '-p', 'Check this image', '--attach', path]);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /Image exceeds the 100 MiB limit/);
  assert.equal(f.requests.length, 0);
});

test('headless goals accept images and continue to completion', async t => {
  const f = await fixture(); t.after(f.cleanup);
  f.env.CODERAI_MODEL_CAPABILITIES = 'image_in';
  const path = join(f.cwd, 'goal.png');
  await writeFile(path, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6UAAAAABJRU5ErkJggg==', 'base64'));
  const result = await f.run(['--trust', '--yolo', '--attach', path, '--json', '-p', '/goal SCENARIO goal']);
  assert.equal(result.code, 0, result.stderr);
  assert.ok(f.requests.some(request => JSON.stringify(request).includes('data:image/png;base64,')));
  assert.match(result.stdout, /"goalStatus":"complete"/);
});
