// Run with: node benchmarks/compare.mjs /absolute/path/node-coderai /absolute/path/bun-coderai
import { fixture } from '../test/fixture.mjs';
import { spawn } from 'node:child_process';
import { stat, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { performance } from 'node:perf_hooks';

const binaries = process.argv.slice(2).map(path => resolve(path));
if (binaries.length !== 2) throw new Error('Provide the preserved Node and Bun executable paths');
const repetitions = Number(process.env.BENCH_RUNS || 5);
if (!Number.isInteger(repetitions) || repetitions < 2) throw new Error('BENCH_RUNS must be an integer >= 2');
const exec = (bin, args, f) => new Promise((done, reject) => {
  const start = performance.now();
  const child = spawn(bin, args, { cwd: f.cwd, env: f.env, stdio: ['pipe', 'pipe', 'pipe'] });
  let stdout = '', stderr = '';
  child.stdout.on('data', x => stdout += x); child.stderr.on('data', x => stderr += x);
  child.on('error', reject); child.on('close', (code, signal) => done({ ms: performance.now() - start, code, signal, stdout, stderr }));
  child.stdin.end();
});
const summary = values => {
  const sorted = [...values].sort((a,b) => a-b), n = sorted.length;
  return { medianMs: +(n % 2 ? sorted[(n-1)/2] : (sorted[n/2-1]+sorted[n/2])/2).toFixed(1), minMs: +sorted[0].toFixed(1), maxMs: +sorted[n-1].toFixed(1) };
};
const samples = binaries.map(() => ({ startup: [], tools: [], sessions: [], resume: [] }));
const rssSamples = binaries.map(() => []);
for (let i = -1; i < repetitions; i++) {
  // Alternate order to reduce drift from changing machine load.
  for (const index of i % 2 === 0 ? [1, 0] : [0, 1]) {
    const bin = binaries[index];
    const f = await fixture();
    try {
      const startup = await exec(bin, ['--help'], f);
      const turn = await exec(bin, ['--trust', '--yolo', '--json', '-p', 'SCENARIO tools'], f);
      const sessions = await exec(bin, ['sessions'], f);
      const resume = await exec(bin, ['--continue', '--json', '-p', 'follow-up'], f);
      const idle = await exec('python3', [resolve('benchmarks/idle.py'), bin], { ...f, env: { ...f.env, TERM: 'xterm-256color' } });
      if (startup.code || !startup.stdout.includes('Usage: coderai') || turn.code || sessions.code || resume.code || idle.code) throw new Error(`Executable failed: ${startup.stderr} ${turn.stderr} ${sessions.stderr} ${resume.stderr} ${idle.stderr}`);
      const events = turn.stdout.trim().split('\n').map(line => JSON.parse(line));
      const resumed = resume.stdout.trim().split('\n').map(line => JSON.parse(line));
      if (events.filter(event => event.type === 'tool.result').length !== 7 || events.at(-1).exitCode !== 0 || resumed.at(-1).exitCode !== 0 || await readFile(join(f.cwd, 'created.txt'), 'utf8') !== 'second\n' || !sessions.stdout.includes('session_')) throw new Error('Workload outcome did not match expected result');
      if (i >= 0) {
        for (const [key, value] of Object.entries({ startup, tools: turn, sessions, resume })) samples[index][key].push(value.ms);
        rssSamples[index].push(JSON.parse(idle.stdout).rssKiB);
      }
    } finally { await f.cleanup(); }
  }
}
for (const [index, bin] of binaries.entries()) {
  console.log(JSON.stringify({ runtime: index ? 'bun' : 'node', binary: bin, bytes: (await stat(bin)).size, warmups: 1, repetitions, idleRssKiB: Object.fromEntries(Object.entries(summary(rssSamples[index])).map(([k, v]) => [k.replace('Ms', ''), v])), ...Object.fromEntries(Object.entries(samples[index]).map(([k,v]) => [k, summary(v)])) }));
}
