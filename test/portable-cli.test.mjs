import { test } from 'node:test';
import { fixture } from './fixture.mjs';
import { checkToolScenario } from './tool-scenario.mjs';

test('built headless CLI runs each workspace tool successfully', { timeout: 30000 }, async t => {
  const f = await fixture();
  t.after(f.cleanup);
  await checkToolScenario(f.run, f);
});
