import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fixture } from './fixture.mjs';

const { shellFailureHint } = await import('../packages/agent-core-v2/src/agent/tools/os/bash/shellFailureHint.ts');

test('missing python alias suggests the project interpreter without treating it as a test failure', () => {
  assert.match(shellFailureHint('/bin/bash: python: command not found\n'), /no tests ran/);
  assert.match(shellFailureHint('/bin/bash: line 1: python: command not found\n'), /python3/);
  assert.match(shellFailureHint('sh: python: not found\n'), /configured interpreter/);
  assert.equal(shellFailureHint('/bin/bash: other: command not found\n'), undefined);
});

test('pytest setup permission errors suggest an equivalent existing runner', () => {
  const output = 'File "/lib/python/site-packages/_pytest/config/findpaths.py", line 53\n' +
    'PermissionError: [Errno 1] Operation not permitted: "/parent/pytest.ini"\n';
  assert.match(shellFailureHint(output), /not evidence of a test assertion failure/);
  assert.match(shellFailureHint(output), /need no pytest fixtures or plugins/);
  assert.match(shellFailureHint(output), /Keep permission boundaries intact/);
  assert.match(shellFailureHint('C:\\lib\\_pytest\\main.py\nPermissionError: access denied'), /unittest/);
  assert.match(shellFailureHint('________________ ERROR collecting tests/test_example.py ________________\n' +
    'File "/lib/_pytest/pathlib.py"\nPermissionError: denied\n'), /setup or collection/);
});

test('ordinary failures and permission errors inside a running test receive no setup advice', () => {
  for (const output of [
    '3 passed in 0.01s',
    'AssertionError: expected 20, got 40\n1 failed, 3 passed',
    'PermissionError: [Errno 1] Operation not permitted: "/private/file"',
    '________________ test_permissions ________________\n' +
      'File "/lib/_pytest/runner.py"\nPermissionError: denied\n',
  ]) assert.equal(shellFailureHint(output), undefined, output);
});

test('shell hints reach the model through the built CLI and preserve command exit status', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const result = await f.run(['--trust', '--yolo', '--json', '-p', 'SCENARIO shell-hints']);
  assert.equal(result.code, 0, result.stderr);
  const results = result.stdout.trim().split('\n').map(line => JSON.parse(line))
    .filter(event => event.type === 'tool.result');
  assert.equal(results.length, 3);
  assert.equal(results[0].isError, true);
  assert.match(results[0].output, /Execution hint:.*python3/);
  assert.equal(results[1].isError, true);
  assert.match(results[1].output, /Execution hint:.*setup or collection/);
  assert.ok(!results[2].isError);
  assert.match(results[2].output, /Execution hint:.*setup or collection/);
  const messages = f.requests.at(-1).messages.filter(message => message.role === 'tool');
  assert.equal(messages.length, 3);
  assert.ok(messages.every(message => JSON.stringify(message.content).includes('Execution hint:')));
});
