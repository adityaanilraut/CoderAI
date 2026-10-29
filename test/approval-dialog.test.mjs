import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'tsx/esm/api';
register();
const { ApprovalDialog } = await import('../apps/coderai/src/tui/approval-dialog.ts');
const { visibleWidth, stripTerminalSequences } = await import('../packages/pi-tui/src/utils.ts');

function setup(rows = 32, preview = 'Example change') {
  const answers = [];
  const dialog = new ApprovalDialog({ toolCallId: 'call', toolName: 'Write', action: 'Write example.txt', display: { kind: 'generic', summary: preview } }, () => rows, answer => answers.push(answer), () => {});
  return { dialog, answers };
}

test('approval defaults to rejection and requires a fresh choice to approve', () => {
  const f = setup();
  f.dialog.handleInput('\r');
  assert.deepEqual(f.answers, ['no']);
  const approve = setup();
  approve.dialog.handleInput('\u001b[B');
  approve.dialog.handleInput('\r');
  assert.deepEqual(approve.answers, ['yes']);
});

test('approval accepts numbered actions and preserves typed feedback', () => {
  for (const [number, expected] of [['1', 'yes'], ['2', 'always'], ['3', '/yolo'], ['4', 'feedback'], ['5', 'no']]) {
    const f = setup();
    f.dialog.handleInput(number); f.dialog.handleInput('\r');
    assert.deepEqual(f.answers, [expected]);
  }
  const f = setup();
  f.dialog.handleInput('no: Use another file'); f.dialog.handleInput('\r');
  assert.deepEqual(f.answers, ['no: Use another file']);
});

test('long approval previews scroll without hiding choices; sizes fit smaller terminals', () => {
  for (const [width, rows] of [[88, 32], [50, 24], [38, 20], [38, 16]]) {
    const f = setup(rows, Array.from({ length: 50 }, (_, i) => `Line ${i}`).join('\n'));
    const before = f.dialog.render(width).map(stripTerminalSequences).join('\n');
    f.dialog.handleInput('\u001b[6~');
    const after = f.dialog.render(width);
    assert.ok(after.length <= rows - 2, `${width}x${rows}: ${after.length} lines`);
    assert.ok(after.every(line => visibleWidth(line) <= width));
    assert.notEqual(after.map(stripTerminalSequences).join('\n'), before);
    assert.match(after.map(stripTerminalSequences).join('\n'), /Enable YOLO/);
    assert.match(after.map(stripTerminalSequences).join('\n'), /Reject with feedback/);
  }
});

test('approval sanitizes terminal sequences before rendering action previews', () => {
  const f = setup(32, 'Before\u001b]52;c;c2VjcmV0\u0007After');
  assert.doesNotMatch(f.dialog.render(88).join('\n'), /52;c;|c2VjcmV0/);
  assert.match(f.dialog.render(88).join('\n'), /BeforeAfter/);
});
