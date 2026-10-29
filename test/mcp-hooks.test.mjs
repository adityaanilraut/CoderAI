import { test } from 'node:test';
import assert from 'node:assert/strict';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixture.mjs';

test('PreToolUse hook receives the tool call and blocks it even in yolo mode', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const hookPath = join(f.root, 'hook.cjs');
  const recordPath = join(f.root, 'hook-input.json');
  await writeFile(hookPath, `let data=''; process.stdin.on('data', c => data += c); process.stdin.on('end', () => { require('fs').writeFileSync(${JSON.stringify(recordPath)}, data); console.error('Blocked by test hook'); process.exit(2); });`);
  const command = `${JSON.stringify(process.execPath)} ${JSON.stringify(hookPath)}`;
  await writeFile(join(f.home, 'config.toml'), `[[hooks]]\nevent = "PreToolUse"\nmatcher = "Write"\ncommand = ${JSON.stringify(command)}\n`);
  const result = await f.run(['--trust', '--yolo', '-p', 'SCENARIO deny', '--json']);
  const input = JSON.parse(await readFile(recordPath, 'utf8'));
  assert.equal(input.hook_event_name, 'PreToolUse');
  assert.match(JSON.stringify(input), /Write/);
  await assert.rejects(access(join(f.cwd, 'denied.txt')), { code: 'ENOENT' });
  assert.match(result.stdout + result.stderr, /Blocked by test hook|blocked/i);
});

test('trusted project MCP server advertises and executes a local tool', { timeout: 30000 }, async t => {
  const f = await fixture(); t.after(f.cleanup);
  const serverPath = join(f.root, 'mcp.cjs');
  await writeFile(serverPath, `
    const readline = require('node:readline');
    readline.createInterface({ input: process.stdin }).on('line', line => {
      const message = JSON.parse(line);
      if (message.id === undefined) return;
      let result;
      if (message.method === 'initialize') result = { protocolVersion: message.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } };
      else if (message.method === 'tools/list') result = { tools: [{ name: 'echo', description: 'Echo text', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] } }] };
      else if (message.method === 'tools/call') result = { content: [{ type: 'text', text: 'MCP_REPLY_' + message.params.arguments.text }] };
      else result = {};
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
    });
  `);
  await mkdir(join(f.cwd, '.coderai'));
  await writeFile(join(f.cwd, '.coderai', 'mcp.json'), JSON.stringify({ mcpServers: { fixture: { command: process.execPath, args: [serverPath] } } }));
  const result = await f.run(['--trust', '--yolo', '-p', 'SCENARIO mcp', '--json']);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /mcp__fixture__echo/);
  assert.match(result.stdout, /MCP_REPLY_mcp-test-value/);
});
