import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, access, readdir, utimes } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { fixture, cli } from './fixture.mjs';

const events = result => result.stdout.trim().split('\n').map(line => JSON.parse(line));
test('built CLI: help, plain output, JSON, pipes, tools, approvals, validation, persistence and cancellation', { timeout: 120000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  await t.test('login only prints local setup guidance without contacting a provider', async () => {
    const count = f.requests.length;
    const result = await f.run(['login']);
    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /does not require an account login/);
    assert.match(result.stdout, /OPENAI_API_KEY/);
    assert.doesNotMatch(result.stdout + result.stderr, /kimi\.com|authorize_device|user_code/);
    assert.equal(f.requests.length, count);
    assert.doesNotMatch((await f.run(['--help'])).stdout, /login|logout/);
  });
  await t.test('OpenAI-compatible defaults accept a standard API key without Kimi credentials', async () => {
    const provider = f.env.CODERAI_MODEL_PROVIDER_TYPE;
    const key = f.env.CODERAI_MODEL_API_KEY;
    const standardKey = f.env.OPENAI_API_KEY;
    delete f.env.CODERAI_MODEL_PROVIDER_TYPE;
    delete f.env.CODERAI_MODEL_API_KEY;
    f.env.OPENAI_API_KEY = 'test-only-key';
    try {
      const result = await f.run(['--trust', '-p', 'hello']);
      assert.equal(result.code, 0, result.stderr);
      assert.match(result.stdout, /deterministic provider/);
      delete f.env.CODERAI_MODEL_NAME;
      const missing = await f.run(['-p', 'hello']);
      assert.equal(missing.code, 1);
      assert.match(missing.stderr, /No model configured.*OPENAI_API_KEY/);
      assert.doesNotMatch(missing.stderr, /Run coderai login/);
    } finally {
      f.env.CODERAI_MODEL_NAME = 'fixture-model';
      f.env.CODERAI_MODEL_PROVIDER_TYPE = provider;
      f.env.CODERAI_MODEL_API_KEY = key;
      if (standardKey === undefined) delete f.env.OPENAI_API_KEY;
      else f.env.OPENAI_API_KEY = standardKey;
    }
  });
  await t.test('help outside the project and missing credentials/config checks', async () => {
    const result = await f.run(['--help']); assert.equal(result.code, 0); assert.match(result.stdout, /Usage: coderai/); assert.doesNotMatch(result.stdout, /\bweb\b|\bacp\b/);
    const key = f.env.CODERAI_MODEL_API_KEY; delete f.env.CODERAI_MODEL_API_KEY;
    const missing = await f.run(['-p', 'hello']); assert.equal(missing.code, 1); assert.match(missing.stderr, /credential|API.key/i); f.env.CODERAI_MODEL_API_KEY = key;
    f.env.CODERAI_MODEL_PROVIDER_TYPE = 'invalid'; const invalid = await f.run(['-p', 'hello']); assert.equal(invalid.code, 1); f.env.CODERAI_MODEL_PROVIDER_TYPE = 'openai';
    await writeFile(join(f.home, 'config.toml'), 'broken = ['); const broken = await f.run(['-p', 'hello']); assert.equal(broken.code, 1); assert.match(broken.stderr, /Invalid TOML syntax/); assert.ok(broken.stderr.includes(join(f.home, 'config.toml')));
    await writeFile(join(f.home, 'config.toml'), 'telemetry = true\n'); const telemetry = await f.run(['-p', 'hello']); assert.equal(telemetry.code, 1); assert.match(telemetry.stderr, /Set telemetry = false/); assert.doesNotMatch(telemetry.stderr, /Invalid TOML/);
    await writeFile(join(f.home, 'config.toml'), 'telemetry = false\n');
  });
  await t.test('documented TOML with environment credentials and model aliases', async () => {
    const overrides = Object.fromEntries(Object.entries(f.env).filter(([key]) => key.startsWith('CODERAI_MODEL_')));
    for (const key of Object.keys(overrides)) delete f.env[key];
    f.env.TEST_PROVIDER_KEY = 'test-only-key';
    await writeFile(join(f.home, 'config.toml'), 'default_model = \"coding\"\ntelemetry = false\n[providers.fixture]\ntype = \"openai\"\nbase_url = \"' + overrides.CODERAI_MODEL_BASE_URL + '\"\napi_key_env = \"TEST_PROVIDER_KEY\"\n[models.coding]\nprovider = \"fixture\"\nmodel = \"fixture-model\"\nmax_context_size = 262144\ncapabilities = []\n[loop_control]\nmax_steps_per_turn = 100\n');
    const result = await f.run(['--trust', '-p', 'hello', '--model', 'coding']); assert.equal(result.code, 0, result.stderr); assert.doesNotMatch(result.stdout + result.stderr, /test-only-key/);
    const bad = await f.run(['-p', 'hello', '-m', 'missing-alias']); assert.equal(bad.code, 1); assert.match(bad.stderr, /Unknown model alias/);
    Object.assign(f.env, overrides); delete f.env.TEST_PROVIDER_KEY; await writeFile(join(f.home, 'config.toml'), 'telemetry = false\n');
  });
  let id;
  await t.test('plain and NDJSON output use the actual streaming engine', async () => {
    const plain = await f.run(['--trust', '-p', 'hello']); assert.equal(plain.code, 0, plain.stderr); assert.equal(plain.stdout, 'Hello from the deterministic provider.\n');
    const json = await f.run(['-p', 'hello', '--json']); assert.equal(json.code, 0, json.stderr);
    const list = events(json); id = list.find(e => e.type === 'coderai.session').sessionId;
    assert.ok(list.some(e => e.type === 'assistant.delta')); assert.equal(list.at(-1).exitCode, 0);
    assert.ok(f.requests.some(r => JSON.stringify(r).includes('TEST_INSTRUCTION_42')));
    assert.ok(f.requests.some(r => JSON.stringify(r).includes('CoderAI')));
  });
  await t.test('no product server, original checkout, or Kimi home is accessed', async () => {
    const previous = f.env.NODE_OPTIONS; f.env.NODE_OPTIONS = `--require=${resolve('test/runtime-guard.cjs')}`;
    const result = await f.run(['--trust', '-p', 'hello']);
    if (previous) f.env.NODE_OPTIONS = previous; else delete f.env.NODE_OPTIONS;
    assert.equal(result.code, 0, result.stderr);
  });
  await t.test('Kimi provider retains its real request and tool path', async () => {
    f.env.CODERAI_MODEL_PROVIDER_TYPE = 'kimi';
    const result = await f.run(['--trust', '-p', 'hello']); assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /deterministic provider/);
    f.env.CODERAI_MODEL_PROVIDER_TYPE = 'openai';
  });
  await t.test('piped content follows explicit prompt separated by a blank line', async () => {
    const result = await f.run(['-p', 'SCENARIO pipe'], 'pipe payload'); assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /SCENARIO pipe\n\npipe payload/);
    assert.equal((await f.run([], 'hello')).code, 0);
    const empty = await f.run([]);
    assert.equal(empty.code, 1);
    assert.match(empty.stderr, /nonempty piped input/);
  });
  await t.test('piped UTF-8 survives byte boundaries and startup input can be cancelled', async () => {
    const child = spawn(process.execPath, [cli, '-p', 'SCENARIO pipe'], { cwd: f.cwd, env: f.env });
    let stdout = ''; let stderr = '';
    child.stdout.on('data', chunk => stdout += chunk);
    child.stderr.on('data', chunk => stderr += chunk);
    const closed = new Promise(resolve => child.on('close', resolve));
    const text = 'hello 🌏 café';
    const bytes = Buffer.from(text);
    child.stdin.write(bytes.subarray(0, 8));
    await new Promise(resolve => setTimeout(resolve, 400));
    child.stdin.end(bytes.subarray(8));
    assert.equal(await closed, 0, stderr);
    assert.ok(stdout.includes(text), stdout);
    for (const signal of ['SIGINT', 'SIGTERM']) {
      const waiting = spawn(process.execPath, [cli, '-p', 'hello'], { cwd: f.cwd, env: f.env });
      waiting.stdout.resume(); waiting.stderr.resume();
      const exited = new Promise(resolve => waiting.on('close', (code, signal) => resolve({ code, signal })));
      await new Promise(resolve => setTimeout(resolve, 800));
      waiting.kill(signal);
      const result = await exited;
      assert.equal(result.code, 130, JSON.stringify(result));
    }
  });
  await t.test('read, glob, grep, write, edit and shell results return to model', async () => {
    const result = await f.run(['-p', 'SCENARIO tools', '--yolo', '--json']); assert.equal(result.code, 0, result.stderr);
    assert.equal(await readFile(join(f.cwd, 'created.txt'), 'utf8'), 'second\n');
    const output = events(result).filter(e => e.type === 'tool.result'); assert.equal(output.length, 7); assert.ok(output.every(e => !e.isError), JSON.stringify(output));
    const requests = f.requests.filter(r => JSON.stringify(r.messages).includes('SCENARIO tools'));
    assert.ok(requests.at(-1).messages.filter(m => m.role === 'tool').some(m => JSON.stringify(m).includes('shell-ok')));
  });
  await t.test('headless denies approvals and validates invalid tool input', async () => {
    const result = await f.run(['-p', 'SCENARIO deny', '--json']); assert.equal(result.code, 1, result.stderr); assert.ok(events(result).some(e => e.type === 'coderai.approval' && e.decision === 'rejected'));
    await assert.rejects(access(join(f.cwd, 'denied.txt')));
    const invalid = await f.run(['-p', 'SCENARIO invalid-tool', '--yolo', '--json']); assert.equal(invalid.code, 0, invalid.stderr); assert.ok(events(invalid).some(e => e.type === 'tool.result' && e.isError));
  });
  await t.test('session history survives process restart and lists saved sessions', async () => {
    const resumed = await f.run(['--resume', id, '-p', 'follow-up', '--json']); assert.equal(resumed.code, 0, resumed.stderr); assert.equal(events(resumed)[0].sessionId, id);
    const list = await f.run(['sessions']); assert.equal(list.code, 0); assert.match(list.stdout, new RegExp(id));
    const locks = await readdir(join(f.home, 'locks'), { recursive: true }); assert.equal(locks.filter(p => p.endsWith('.lock')).length, 0);
    assert.ok(f.requests.at(-1).messages.filter(m => m.role === 'assistant').length > 0);
  });
  await t.test('--continue resumes the latest conversation only for this workspace', async () => {
    const fresh = await f.run(['-p', 'continue first', '--json']);
    assert.equal(fresh.code, 0, fresh.stderr);
    const latest = events(fresh)[0].sessionId;
    const resumed = await f.run(['--continue', '-p', 'continue second', '--json']);
    assert.equal(resumed.code, 0, resumed.stderr);
    assert.equal(events(resumed)[0].sessionId, latest);
    assert.ok(f.requests.at(-1).messages.some(m => JSON.stringify(m).includes('continue first')));
    const conflict = await f.run(['--continue', '--resume', latest, '-p', 'hello']);
    assert.equal(conflict.code, 1);
    assert.match(conflict.stderr, /Cannot combine --continue with --resume/);
    const other = join(f.root, 'another-workspace');
    const { mkdir } = await import('node:fs/promises');
    await mkdir(other);
    const isolated = await f.run(['--cwd', other, '--trust', '--continue', '-p', 'new workspace', '--json']);
    assert.equal(isolated.code, 0, isolated.stderr);
    assert.notEqual(events(isolated)[0].sessionId, latest);
    assert.match(isolated.stderr, /No saved conversation/);
  });
  await t.test('out-of-workspace reads follow Kimi defaults and background children stop on exit', async () => {
    await writeFile(join(f.root, 'outside.txt'), 'private outside content');
    const outside = await f.run(['-p', 'SCENARIO outside', '--json']);
    assert.equal(outside.code, 0, outside.stderr);
    assert.ok(!events(outside).some(e => e.type === 'coderai.approval'));
    assert.ok(f.requests.filter(r => JSON.stringify(r.messages).includes('SCENARIO outside')).some(r => JSON.stringify(r.messages).includes('private outside content')));
    const bg = await f.run(['-p', 'SCENARIO background', '--yolo', '--json']); assert.equal(bg.code, 0, bg.stderr);
    assert.ok(events(bg).some(e => e.type === 'background.task.started'));
    const pid = Number(await readFile(join(f.cwd, 'background.pid'), 'utf8'));
    assert.throws(() => process.kill(pid, 0));
  });
  await t.test('background commands can be listed, inspected and explicitly stopped', async () => {
    const result = await f.run(['-p', 'SCENARIO tasks', '--yolo', '--json']); assert.equal(result.code, 0, result.stderr);
    const output = events(result).filter(e => e.type === 'tool.result'); assert.equal(output.length, 4); assert.ok(output.every(e => !e.isError), JSON.stringify(output));
    assert.ok(events(result).some(e => e.type === 'background.task.terminated'));
    const pid = Number(await readFile(join(f.cwd, 'tracked.pid'), 'utf8')); assert.throws(() => process.kill(pid, 0));
  });
  await t.test('opt-in background drain waits for a task and emits its completion', async () => {
    const result = await f.run(['-p', 'SCENARIO background-short', '--yolo', '--json', '--wait-background']);
    assert.equal(result.code, 0, result.stderr);
    assert.match(await readFile(join(f.cwd, 'background-finished.txt'), 'utf8'), /done/);
    assert.ok(events(result).some(event => event.type === 'background.task.terminated'));
    assert.equal(events(result).at(-1).type, 'coderai.result');
  });
  await t.test('headless goal follows continuations and reports completion', async () => {
    const result = await f.run(['-p', '/goal SCENARIO goal', '--json']);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(events(result).at(-1).goalStatus, 'complete');
    assert.match(result.stdout, /Goal finished/);
  });
  await t.test('auth and unavailable model errors have nonzero exit codes', async () => {
    for (const scenario of ['auth', 'missing-model']) { const result = await f.run(['-p', `SCENARIO ${scenario}`]); assert.equal(result.code, 1, result.stderr); }
  });
  await t.test('concurrent session writers are rejected and SIGTERM cancels a shell', async () => {
    const child = spawn(process.execPath, [cli, '--resume', id, '-p', 'SCENARIO cancel-shell', '--yolo', '--json'], { cwd: f.cwd, env: f.env });
    child.stdin.end(); let stderr = ''; child.stdout.resume(); child.stderr.on('data', c => stderr += c);
    await new Promise((resolve, reject) => { const poll = setInterval(async () => { try { await access(join(f.cwd, 'shell.pid')); clearInterval(poll); resolve(); } catch {} }, 20); setTimeout(() => { clearInterval(poll); reject(new Error(stderr)); }, 5000).unref(); });
    const conflict = await f.run(['--resume', id, '-p', 'conflicting']); assert.equal(conflict.code, 1); assert.match(conflict.stderr, /in use/);
    child.kill('SIGTERM'); const code = await new Promise(resolve => child.on('close', resolve)); assert.equal(code, 130, stderr);
    const pid = Number(await readFile(join(f.cwd, 'shell.pid'), 'utf8')); assert.throws(() => process.kill(pid, 0));
  });
  await t.test('an abandoned session lock is recovered without losing conversation history', async () => {
    const child = spawn(process.execPath, [cli, '--resume', id, '-p', 'SCENARIO wait', '--json'], { cwd: f.cwd, env: f.env });
    child.stdin.end(); child.stdout.resume(); let stderr = ''; child.stderr.on('data', c => stderr += c);
    const closed = new Promise(resolve => child.on('close', (code, signal) => resolve({ code, signal })));
    await new Promise((resolve, reject) => {
      const poll = setInterval(() => {
        if (f.requests.some(r => JSON.stringify(r).includes('SCENARIO wait'))) { clearInterval(poll); resolve(); }
      }, 20);
      setTimeout(() => { clearInterval(poll); reject(new Error(stderr)); }, 5000).unref();
    });
    child.kill('SIGKILL');
    assert.equal((await closed).signal, 'SIGKILL');
    const lock = join(f.home, 'locks', `${id}.lock.lock`);
    await access(lock);
    const stale = new Date(Date.now() - 30000);
    await utimes(lock, stale, stale);
    const resumed = await f.run(['--resume', id, '-p', 'recovered', '--json']);
    assert.equal(resumed.code, 0, resumed.stderr);
    assert.equal(events(resumed)[0].sessionId, id);
    assert.ok(f.requests.at(-1).messages.some(m => m.role === 'assistant'));
    await assert.rejects(access(lock));
  });
  await t.test('SIGINT cancels an active model request and releases locks', async () => {
    const requestCount = f.requests.length;
    const child = spawn(process.execPath, [cli, '-p', 'SCENARIO wait', '--json'], { cwd: f.cwd, env: f.env });
    child.stdin.end(); let stdout = ''; child.stdout.on('data', c => stdout += c); let stderr = ''; child.stderr.on('data', c => stderr += c);
    await new Promise((resolve, reject) => { const poll = setInterval(() => { if (f.requests.slice(requestCount).some(r => JSON.stringify(r).includes('SCENARIO wait'))) { clearInterval(poll); resolve(); } }, 20); setTimeout(() => { clearInterval(poll); reject(new Error(stderr)); }, 5000).unref(); });
    child.kill('SIGINT'); const code = await new Promise(resolve => child.on('close', resolve)); assert.equal(code, 130, stderr); assert.equal(events({ stdout }).at(-1).exitCode, 130);
    assert.equal((await readdir(join(f.home, 'locks'), { recursive: true })).filter(p => p.endsWith('.lock')).length, 0);
  });
  await t.test('cancellation kills a shell that ignores SIGTERM', async () => {
    const child = spawn(process.execPath, [cli, '-p', 'SCENARIO stubborn-shell', '--yolo', '--json'], { cwd: f.cwd, env: f.env });
    child.stdin.end(); child.stdout.resume(); let stderr = ''; child.stderr.on('data', c => stderr += c);
    const exited = new Promise(resolve => child.on('close', (code, signal) => resolve({ code, signal })));
    await new Promise((resolve, reject) => { const poll = setInterval(async () => { try { await access(join(f.cwd, 'stubborn.pid')); clearInterval(poll); resolve(); } catch {} }, 20); setTimeout(() => { clearInterval(poll); reject(new Error(stderr)); }, 5000).unref(); });
    const pid = Number(await readFile(join(f.cwd, 'stubborn.pid'), 'utf8'));
    try {
      child.kill('SIGTERM');
      const result = await exited;
      assert.equal(result.code, 130, stderr);
      assert.doesNotMatch(stderr, /Shutdown timed out/);
      assert.throws(() => process.kill(pid, 0));
    } finally { try { process.kill(pid, 'SIGKILL'); } catch {} }
  });
});
