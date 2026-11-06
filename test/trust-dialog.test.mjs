import { test } from 'node:test';
import assert from 'node:assert/strict';
const { TrustDialog } = await import('../apps/coderai/src/tui/trust-dialog.ts');
const { visibleWidth, stripTerminalSequences } = await import('../packages/pi-tui/src/utils.ts');

function setup({ path = '/Users/aditya/Desktop/untitled folder 2', rows = 32, startup = true } = {}) {
  const answers = [];
  const dialog = new TrustDialog(path, startup, () => rows, answer => answers.push(answer), () => {});
  return { dialog, answers };
}

test('workspace trust defaults to exit and requires an explicit trust choice', () => {
  const f = setup();
  f.dialog.handleInput('\r');
  assert.deepEqual(f.answers, ['']);
  const trust = setup();
  trust.dialog.handleInput('\u001b[A'); trust.dialog.handleInput('\r');
  assert.deepEqual(trust.answers, ['yes']);
  for (const [reply, expected] of [['1', 'yes'], ['yes', 'yes'], ['YES', 'yes'], ['2', ''], ['no', ''], ['anything', '']]) {
    const typed = setup();
    typed.dialog.handleInput(reply); typed.dialog.handleInput('\r');
    assert.deepEqual(typed.answers, [expected]);
  }
});

test('Esc cancels a typed trust answer and workspace switching uses Cancel', () => {
  const f = setup({ startup: false });
  f.dialog.handleInput('yes'); f.dialog.handleInput('\u001b');
  assert.deepEqual(f.answers, ['']);
  assert.match(f.dialog.render(88).map(stripTerminalSequences).join('\n'), /Cancel without trusting/);
});

test('trust details wrap and scroll in small terminals while choices remain visible', () => {
  for (const [width, rows] of [[88, 32], [50, 24], [38, 16]]) {
    const f = setup({ rows, path: '/workspace/' + 'long folder name/'.repeat(40) });
    const before = f.dialog.render(width).map(stripTerminalSequences).join('\n');
    f.dialog.handleInput('\u001b[6~');
    const after = f.dialog.render(width);
    assert.ok(after.length <= rows - 2);
    assert.ok(after.every(line => visibleWidth(line) <= width));
    assert.notEqual(after.map(stripTerminalSequences).join('\n'), before);
    assert.match(after.map(stripTerminalSequences).join('\n'), /Trust and continue/);
    assert.match(after.map(stripTerminalSequences).join('\n'), /Exit without trusting/);
    assert.doesNotMatch(before, /Waiting for approval|YOLO/);
  }
});

test('workspace trust sanitizes terminal sequences in directory names', () => {
  const f = setup({ path: '/workspace/before\u001b]52;c;c2VjcmV0\u0007after' });
  const text = f.dialog.render(88).join('\n');
  assert.match(text, /beforeafter/);
  assert.doesNotMatch(text, /52;c;|c2VjcmV0/);
});

test('workspace trust renders the project-specific disclosure', () => {
  const dialog = new TrustDialog('/project', true, () => 40, () => {}, () => {}, 'Trust /project?\nMCP servers:\n  local-server\nSkills:\n  project-skill');
  const text = dialog.render(100).map(stripTerminalSequences).join('\n');
  assert.match(text, /local-server/);
  assert.match(text, /project-skill/);
});
