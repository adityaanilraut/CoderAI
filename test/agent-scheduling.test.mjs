import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { after, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { build } from 'esbuild';
import { engineBundlePlugins } from '../scripts/bundle-options.mjs';

const directory = await mkdtemp(resolve('node_modules/.scheduler-'));
after(() => rm(directory, { recursive: true, force: true }));
const entry = join(directory, 'scheduler.mjs');
await build({
  stdin: { resolveDir: process.cwd(), contents: `
    export { computeNextCronRun, parseCronExpression } from './packages/agent-core-v2/src/features/cron/internal/cron-expr.ts';
    export { AgentRunBatch } from './packages/agent-core-v2/src/features/swarm/session/agentRunBatch.ts';
    export { APIStatusError, parseRetryAfterMs } from './packages/agent-core-v2/src/llm-adapter/contract/errors.ts';
  ` },
  outfile: entry, bundle: true, platform: 'node', format: 'esm', target: 'node24',
  plugins: engineBundlePlugins(),
});
const { computeNextCronRun, parseCronExpression, AgentRunBatch, APIStatusError, parseRetryAfterMs } = await import(entry);

test('Retry-After accepts HTTP dates and keeps oversized values finite', () => {
  const parse = value => parseRetryAfterMs(new Headers({ 'Retry-After': value }));
  const time = new Date(Math.floor(Date.now() / 1000) * 1000 + 5000);
  const before = Date.now();
  const remaining = parse(time.toUTCString());
  assert.ok(remaining <= time.getTime() - before);
  assert.ok(remaining >= time.getTime() - Date.now());
  assert.equal(parse('5'), 5000);
  assert.equal(parse('0.1'), null);
  assert.equal(parse('999999999999999999999999'), Number.MAX_SAFE_INTEGER);
});

test('cron candidates remain monotonic through daylight-saving transitions', () => {
  const previous = process.env.TZ;
  process.env.TZ = 'America/New_York';
  try {
    const next = (expression, from) => new Date(computeNextCronRun(parseCronExpression(expression), Date.parse(from))).toISOString();
    assert.equal(next('* * * * *', '2025-11-02T06:10:00Z'), '2025-11-02T06:11:00.000Z');
    assert.equal(next('30 1 * * *', '2025-11-02T05:31:00Z'), '2025-11-02T06:30:00.000Z');
    assert.equal(next('30 2 * * *', '2025-03-09T06:59:00Z'), '2025-03-10T06:30:00.000Z');
  } finally {
    if (previous === undefined) delete process.env.TZ;
    else process.env.TZ = previous;
  }
});

test('Swarm keeps its configured concurrency limit after a rate limit', { timeout: 15000 }, async () => {
  let active = 0;
  let peak = 0;
  let retries = 0;
  const handle = (agentId, options, retry) => {
    active++;
    peak = Math.max(peak, active);
    options.onReady?.();
    return {
      agentId, profileName: 'agent',
      completion: (async () => {
        try {
          await delay(retry ? 3400 : 10, undefined, { signal: options.signal });
          if (agentId === 'limited' && !retry) throw new APIStatusError(429, 'Fixture rate limit');
          return { result: agentId };
        } finally { active--; }
      })(),
    };
  };
  const launcher = {
    async spawn(options) { return handle(options.prompt, options, false); },
    async retry(agentId, options) { retries++; return handle(agentId, options, true); },
  };
  const tasks = ['first', 'second', 'third', 'limited', 'last'].map(prompt => ({
    kind: 'spawn', data: prompt, plan: {}, profileName: 'agent', parentToolCallId: 'parent',
    prompt, description: prompt, runInBackground: false,
  }));
  const results = await new AgentRunBatch(launcher, tasks, { maxConcurrency: 1 }).run();
  assert.equal(retries, 1);
  assert.equal(peak, 1);
  assert.ok(results.every(result => result.status === 'completed'));
});
