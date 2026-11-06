import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

export const testRuntime = process.env.CODERAI_TEST_RUNTIME || 'bun';
export const cli = resolve('dist/coderai.mjs');
export async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'coderai-test-'));
  const home = join(root, 'home'); const cwd = join(root, 'project');
  await mkdir(home); await mkdir(cwd);
  await writeFile(join(cwd, 'input.txt'), 'hello fixture\n');
  await writeFile(join(cwd, 'AGENTS.md'), 'Use the fixture convention: TEST_INSTRUCTION_42.\n');
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : {};
    requests.push(body);
    const serialized = JSON.stringify(body.messages ?? []);
    if (serialized.includes('SCENARIO ui-retry') && requests.filter(request => JSON.stringify(request.messages ?? []).includes('SCENARIO ui-retry')).length === 1) {
      res.writeHead(429, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'Fixture rate limit; retry shortly', type: 'rate_limit_error' } })); return;
    }
    if (serialized.includes('SCENARIO terminal-escape')) {
      const chunk = content => ({ id: 'fixture', object: 'chat.completion.chunk', created: 1, model: body.model, choices: [{ index: 0, delta: { content }, finish_reason: null }] });
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      for (const part of ['Safe ', '\u001b', ']52;c;TERMINAL_INJECTION', '\u0007', ' text']) {
        res.write(`data: ${JSON.stringify(chunk(part))}\n\n`);
        await new Promise(resolve => setTimeout(resolve, 40));
      }
      res.write(`data: ${JSON.stringify({ ...chunk(''), choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\n`);
      res.end('data: [DONE]\n\n');
      return;
    }
    if (serialized.includes('SCENARIO auth')) { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'Invalid test credential', type: 'authentication_error' } })); return; }
    if (serialized.includes('SCENARIO missing-model')) { res.writeHead(404, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'Model does not exist', type: 'invalid_request_error', code: 'model_not_found' } })); return; }
    if (serialized.includes('SCENARIO goal-pause') && requests.filter(r => JSON.stringify(r.messages ?? []).includes('SCENARIO goal-pause')).length === 1) { req.socket.on('close', () => res.destroy()); return; }
    if (serialized.includes('SCENARIO wait') && !serialized.includes('recovered') && !serialized.includes('This is a side-channel conversation with the user.')) { req.socket.on('close', () => res.destroy()); return; }
    let content = 'Hello from the deterministic provider.';
    let call;
    const messages = body.messages ?? [];
    const results = messages.filter(m => m.role === 'tool');
    const latestUser = messages.findLastIndex(message => message.role === 'user' && JSON.stringify(message.content).includes('SCENARIO ui-todo'));
    const latestPrompt = JSON.stringify(messages[latestUser]?.content ?? '');
    const turnResults = messages.slice(latestUser + 1).filter(message => message.role === 'tool');
    const uiTodos = [
      { title: 'Finished earlier', status: 'done' },
      { title: 'Latest finished', status: 'done' },
      { title: 'Next pending item', status: 'pending' },
      { title: 'Active task', status: 'in_progress' },
      { title: 'Last pending item', status: 'pending' },
    ];
    const phase5Index = messages.findLastIndex(message => message.role === 'user' && JSON.stringify(message.content).includes('SCENARIO phase5-'));
    const phase5Prompt = JSON.stringify(messages[phase5Index]?.content ?? '');
    const phase5Results = messages.slice(phase5Index + 1).filter(message => message.role === 'tool');
    if (phase5Prompt.includes('SCENARIO phase5-write')) {
      call = [['Write', { path: 'input.txt', content: 'changed first\n' }], ['Write', { path: 'created.txt', content: 'first\n' }]][phase5Results.length];
      content = 'First file turn complete.';
    } else if (phase5Prompt.includes('SCENARIO phase5-edit')) {
      call = [['Edit', { path: 'created.txt', old_string: 'first', new_string: 'second' }], ['Write', { path: 'later.txt', content: 'later\n' }]][phase5Results.length];
      content = 'Second file turn complete.';
    } else if (phase5Prompt.includes('SCENARIO phase5-delete')) {
      call = [['Edit', { path: 'input.txt', old_string: 'hello', new_string: 'updated' }], ['Bash', { command: 'rm input.txt' }]][phase5Results.length];
      content = 'Deletion turn complete.';
    } else if (serialized.includes('SCENARIO phase6-batch')) {
      if (!results.length) call = ['AskUserQuestion', { questions: [
        { question: 'Choose first? A long question body that wraps across narrow terminals.', header: 'First', options: [{ label: 'Alpha' }, { label: 'Beta' }], multi_select: false },
        { question: 'Choose second?', header: 'Second', options: [{ label: 'One' }, { label: 'Two' }], multi_select: false },
      ] }];
      content = 'Batch answers received.';
    } else if (serialized.includes('SCENARIO phase6-mermaid')) {
      content = 'Diagram fixture\n\n```mermaid\nflowchart LR\n  Alpha --> Beta\n```\n\n[Fixture link](https://example.test/coderai)\n' + 'Transcript scroll marker\n\n'.repeat(30);
    } else if (serialized.includes('SCENARIO phase4-notify')) {
      if (!results.length) call = ['NotifyUser', { title: 'Test progress', message: 'The interactive update reached the user.' }];
      content = 'Notification fixture completed.';
    } else if (serialized.includes('SCENARIO phase4-cron')) {
      if (!results.length) call = ['CronCreate', { cron: '0 0 1 1 *', prompt: 'Scheduled fixture', recurring: false }];
      content = 'Scheduled fixture created.';
    } else if (serialized.includes('SCENARIO ui-transcript')) {
      if (results.length < 2) call = ['Read', { path: results.length ? 'AGENTS.md' : 'input.txt' }];
      content = 'Transcript fixture complete.';
    } else if (latestPrompt.includes('SCENARIO ui-todos')) {
      if (!turnResults.length) call = ['TodoList', { todos: uiTodos }];
      content = 'Todo fixture ready.';
    } else if (latestPrompt.includes('SCENARIO ui-todo-complete')) {
      if (!turnResults.length) call = ['TodoList', { todos: uiTodos.map(todo => ({ ...todo, status: 'done' })) }];
      content = 'Todo fixture completed.';
    } else if (latestPrompt.includes('SCENARIO ui-todo-clear')) {
      if (!turnResults.length) call = ['TodoList', { todos: [] }];
      content = 'Todo fixture cleared.';
    } else if (serialized.includes('SCENARIO shell-hints')) {
      const pytestError = String.raw`printf 'File "/lib/_pytest/config/findpaths.py"\nPermissionError: Operation not permitted: "/parent/pytest.ini"\n'`;
      const sequence = [
        ['Bash', { command: 'PATH=/coderai-fixture-empty-path python -c pass' }],
        ['Bash', { command: `${pytestError}; exit 2` }],
        ['Bash', { command: `(${pytestError}; exit 2) | tail -5` }],
      ];
      call = sequence[results.length]; content = 'Shell diagnostics complete.';
    } else if (serialized.includes('SCENARIO tools')) {
      const sequence = [
        ['Read', { path: 'input.txt' }],
        ['Glob', { pattern: '*.txt' }],
        ['Grep', { pattern: 'hello', path: '.', output_mode: 'content' }],
        ['Write', { path: 'created.txt', content: 'first\n' }],
        ['Read', { path: 'created.txt' }],
        ['Edit', { path: 'created.txt', old_string: 'first', new_string: 'second' }],
        ['Bash', { command: 'printf shell-ok', timeout: 5 }],
      ];
      call = sequence[results.length]; content = 'Tools complete.';
    } else if (serialized.includes('SCENARIO goal-pause')) {
      if (results.length === 0) call = ['UpdateGoal', { status: 'complete' }];
      content = 'Resumed goal finished.';
    } else if (serialized.includes('SCENARIO goal')) {
      if (serialized.includes('Continue working toward the active goal.') && results.length === 0) call = ['UpdateGoal', { status: 'complete' }];
      content = results.length ? 'Goal finished.' : 'Goal progress.';
    } else if (serialized.includes('SCENARIO multi-question')) {
      if (results.length === 0) call = ['AskUserQuestion', { questions: [{ question: 'Choose colors?', header: 'Colors', options: [{ label: 'Red', description: 'Warm' }, { label: 'Blue', description: 'Cool' }], multi_select: true }] }];
      content = 'Question answered.';
    } else if (serialized.includes('SCENARIO question')) {
      if (results.length === 0) call = ['AskUserQuestion', { questions: [{ question: 'Choose a color?', header: 'Color', options: [{ label: 'Red', description: 'Warm' }, { label: 'Blue', description: 'Cool' }], multi_select: false }] }];
      content = 'Question answered.';
    } else if (serialized.includes('SCENARIO mcp')) {
      if (results.length === 0) call = ['mcp__fixture__echo', { text: 'mcp-test-value' }];
      content = 'MCP tool completed.';
    } else if (serialized.includes('SCENARIO deny')) {
      if (serialized.includes('SCENARIO deny-draft') && results.length === 0) await new Promise(resolve => setTimeout(resolve, 1000));
      if (results.length === 0) call = ['Write', { path: 'denied.txt', content: 'should not exist' }];
      content = 'The requested action was denied.';
    } else if (serialized.includes('SCENARIO ui-task-output')) {
      if (results.length === 0) call = ['Bash', { command: "i=1; while [ $i -le 60 ]; do printf 'LOG-%03d\\n' $i; i=$((i+1)); done; sleep 5; echo LIVE-TAIL; sleep 20", description: 'output viewer fixture', run_in_background: true, timeout: 30 }];
      content = 'Started output fixture.';
    } else if (serialized.includes('SCENARIO background-short')) {
      if (results.length === 0) call = ['Bash', { command: 'sleep 1; echo done > background-finished.txt', description: 'test background drain', run_in_background: true, timeout: 10 }];
      content = 'Started short background task.';
    } else if (serialized.includes('SCENARIO background')) {
      if (results.length === 0) call = ['Bash', { command: 'echo $$ > background.pid; exec sleep 30', description: 'test background cleanup', run_in_background: true, timeout: 45 }];
      content = 'Started background task.';
    } else if (serialized.includes('SCENARIO tasks')) {
      const taskId = JSON.stringify(results).match(/task_id:\s*([a-zA-Z0-9_-]+)/)?.[1];
      const sequence = [
        ['Bash', { command: 'echo $$ > tracked.pid; exec sleep 30', description: 'tracked fixture command', run_in_background: true, timeout: 45 }],
        ['TaskList', {}], ['TaskOutput', { task_id: taskId }], ['TaskStop', { task_id: taskId }],
      ];
      call = sequence[results.length]; content = 'Background task inspected and stopped.';
    } else if (serialized.includes('SCENARIO cancel-shell')) {
      if (results.length === 0) call = ['Bash', { command: 'echo $$ > shell.pid; exec sleep 30', timeout: 45 }];
      content = 'Shell completed.';
    } else if (serialized.includes('SCENARIO stubborn-shell')) {
      if (results.length === 0) call = ['Bash', { command: "trap '' TERM; echo $$ > stubborn.pid; exec sleep 30", timeout: 45 }];
      content = 'Shell completed.';
    } else if (serialized.includes('SCENARIO invalid-tool')) {
      if (results.length === 0) call = ['Write', { path: 'missing-content.txt' }];
      content = 'Validation returned to model.';
    } else if (serialized.includes('SCENARIO outside')) {
      if (results.length === 0) call = ['Read', { path: join(root, 'outside.txt') }];
      content = 'Outside read handled.';
    } else if (serialized.includes('SCENARIO symlink')) {
      if (results.length === 0) call = ['Read', { path: 'outside-link.txt' }];
      content = 'Symlink read handled.';
    } else if (serialized.includes('SCENARIO pipe')) content = messages.filter(m => m.role === 'user').map(m => typeof m.content === 'string' ? m.content : JSON.stringify(m.content)).join('\n');
    if (call && !(body.tools ?? []).some(t => t.function?.name === call[0])) {
      res.writeHead(500); res.end(`Tool not advertised: ${call[0]}`); return;
    }
    const delta = call ? { role: 'assistant', tool_calls: [{ index: 0, id: `call_${results.length}`, type: 'function', function: { name: call[0], arguments: JSON.stringify(call[1]) } }] } : { role: 'assistant', content };
    const chunk = d => ({ id: 'fixture', object: 'chat.completion.chunk', created: 1, model: body.model, choices: [{ index: 0, delta: d, finish_reason: null }] });
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    if (serialized.includes('SCENARIO ui-transcript') && results.length === 0) {
      res.write(`data: ${JSON.stringify(chunk({ reasoning_content: 'Reviewing the fixture files before answering.' }))}\n\n`);
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    res.write(`data: ${JSON.stringify(chunk(delta))}\n\n`);
    res.write(`data: ${JSON.stringify({ ...chunk({}), choices: [{ index: 0, delta: {}, finish_reason: call ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 } })}\n\n`);
    res.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (key.startsWith('KIMI_') || key.startsWith('CODERAI_')) delete env[key];
  Object.assign(env, { CODERAI_HOME: home, CODERAI_MODEL_NAME: 'fixture-model', CODERAI_MODEL_PROVIDER_TYPE: 'openai', CODERAI_MODEL_API_KEY: 'test-only-key', CODERAI_MODEL_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, CODERAI_MODEL_CAPABILITIES: 'none', KIMI_LOG_LEVEL: 'off', NO_COLOR: '1' });
  const run = (args = [], input = '') => new Promise((resolveRun, reject) => {
    const child = spawn(testRuntime, [cli, ...args], { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = ''; let stderr = '';
    child.stdout.on('data', c => stdout += c); child.stderr.on('data', c => stderr += c);
    child.on('error', reject); child.on('close', (code, signal) => resolveRun({ code, signal, stdout, stderr }));
    child.stdin.end(input);
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`CLI timed out\n${stdout}\n${stderr}`)); }, 20000);
    timer.unref(); child.on('close', () => clearTimeout(timer));
  });
  return { root, home, cwd, env, requests, run, server, cleanup: async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(root, { recursive: true, force: true }); } };
}
