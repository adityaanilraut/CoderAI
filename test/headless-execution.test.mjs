import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test } from 'node:test';
import { fixture } from './fixture.mjs';

function finalResult(result) {
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.filter(event => event.type === 'coderai.result').length, 1, result.stdout);
  assert.equal(events.at(-1).type, 'coderai.result', result.stdout);
  assert.equal(events.at(-1).exitCode, result.code);
  return { events, final: events.at(-1) };
}

test('a prompt blocked by a submission hook exits with one final JSON result', { timeout: 30000 }, async t => {
  const f = await fixture();
  t.after(f.cleanup);
  await writeFile(join(f.home, 'config.toml'), [
    '[[hooks]]',
    'event = "UserPromptSubmit"',
    'command = "echo fixture-prompt-blocked >&2; exit 2"',
    '',
  ].join('\n'));

  const result = await f.run(['--trust', '--json', '-p', 'Inspect the fixture']);
  assert.equal(result.code, 1, result.stdout + result.stderr);
  const { events, final } = finalResult(result);
  assert.equal(final.reason, 'blocked');
  assert.ok(events.some(event => event.type === 'hook.result' && JSON.stringify(event).includes('fixture-prompt-blocked')));
  assert.ok(events.every(event => event.type !== 'turn.started'));
  assert.equal(f.requests.length, 0);
});

for (const mode of ['configured', 'explicit']) {
  test(`${mode} background drain reports a timeout for unfinished work`, { timeout: 30000 }, async t => {
    const f = await fixture();
    t.after(f.cleanup);
    f.env.KIMI_CODE_BACKGROUND_PRINT_BACKGROUND_MODE = mode === 'configured' ? 'drain' : 'exit';
    f.env.KIMI_CODE_BACKGROUND_PRINT_WAIT_CEILING_S = '1';
    const wait = mode === 'explicit' ? ['--wait-background'] : [];
    const result = await f.run(['--trust', '--yolo', '--json', ...wait, '-p', 'SCENARIO background']);
    assert.equal(result.code, 1, result.stdout + result.stderr);
    const { events, final } = finalResult(result);
    assert.ok(events.some(event => event.type === 'background.task.started'));
    assert.equal(final.reason, 'background_timeout');
  });
}
