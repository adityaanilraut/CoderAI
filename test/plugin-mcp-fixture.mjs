import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export async function checkNodePlugin(fixture, run) {
  const root = join(fixture.root, 'node-plugin');
  await mkdir(root);
  await writeFile(join(root, 'kimi.plugin.json'), JSON.stringify({
    name: 'node-fixture',
    mcpServers: { demo: { command: 'node', args: ['./server.cjs'] } },
  }));
  await writeFile(join(root, 'server.cjs'), `
    if (process.versions.bun) throw new Error('The plugin requires Node.js.');
    require('node:readline').createInterface({ input: process.stdin }).on('line', (line) => {
      const message = JSON.parse(line);
      if (message.id === undefined) return;
      const result = message.method === 'initialize'
        ? { protocolVersion: message.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'node-fixture', version: '1' } }
        : { tools: [{ name: 'node_fixture_tool', inputSchema: { type: 'object' } }] };
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
    });
  `);

  const installed = await run(['plugins', 'install', root]);
  assert.equal(installed.code, 0, installed.stderr);
  const tested = await run(['--trust', 'mcp', 'test', 'plugin-node-fixture:demo']);
  assert.equal(tested.code, 0, tested.stderr);
  assert.match(tested.stdout, /node_fixture_tool/);
}
