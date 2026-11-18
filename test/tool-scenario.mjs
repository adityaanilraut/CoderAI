import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

export async function checkToolScenario(run, fixture) {
  const result = await run(['--trust', '--yolo', '--json', '-p', 'SCENARIO tools']);
  assert.equal(result.code, 0, result.stderr || `signal: ${result.signal}`);
  const events = result.stdout.trim().split('\n').map(line => JSON.parse(line));
  assert.equal(events.at(-1).exitCode, 0, result.stderr);
  const tools = events.filter(event => event.type === 'tool.result');
  assert.equal(tools.length, 7);
  assert.ok(tools.every(event => !event.isError), JSON.stringify(tools));
  assert.ok(tools.some(event => JSON.stringify(event.output)?.includes('shell-ok')), JSON.stringify(tools));
  assert.equal(await readFile(join(fixture.cwd, 'created.txt'), 'utf8'), 'second\n');
}
