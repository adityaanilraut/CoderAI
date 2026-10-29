import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { McpServerConfigSchema } from '../../../packages/agent-core-v2/src/mcpCore/config-schema.js';
import type { Runtime } from './runtime.js';

/** List the merged configuration without printing credential values. */
export async function listMcpConfiguration(runtime: Runtime): Promise<string> {
  if (runtime.options.trust) await runtime.trust(async () => false);
  const trust = await runtime.harness.getWorkspaceTrustInfo(runtime.options.cwd);
  const servers = await runtime.harness.listMcpServers({ cwd: runtime.options.cwd });
  const listing = servers.length ? servers.map(server => {
    let target: string;
    if (server.transport === 'stdio') target = server.command;
    else { const url = new URL(server.url); url.username = ''; url.password = ''; url.search = ''; url.hash = ''; target = url.toString(); }
    return `${server.name}\t${server.transport}\t${server.source}\t${server.mutable ? 'editable' : 'read-only'}\t${target}`;
  }).join('\n') : 'No MCP servers configured.';
  return trust.trusted ? listing : `${listing}\nProject servers are hidden until the workspace is trusted. Use --trust to trust this workspace.`;
}

/** Add or replace a user-level server from a JSON configuration file. */
export async function writeMcpConfiguration(runtime: Runtime, operation: 'add' | 'update', name: string, path: string): Promise<void> {
  if (!name.trim()) throw new Error('MCP server name cannot be empty.');
  let input: unknown;
  try { input = JSON.parse(await readFile(resolve(path), 'utf8')); }
  catch (error) { throw new Error(`Cannot read MCP configuration JSON: ${error instanceof Error ? error.message : String(error)}`); }
  const parsed = McpServerConfigSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Invalid MCP server configuration: ${parsed.error.issues.map(issue => `${issue.path.join('.') || 'config'}: ${issue.message}`).join('; ')}`);
  const server = { name, ...parsed.data };
  if (operation === 'add') await runtime.harness.addMcpServer(server, { cwd: runtime.options.cwd });
  else await runtime.harness.updateMcpServer(server, { cwd: runtime.options.cwd });
}

export async function removeMcpConfiguration(runtime: Runtime, name: string): Promise<void> {
  await runtime.harness.removeMcpServer(name, { cwd: runtime.options.cwd });
}

/** Testing a configured process can execute project configuration, so use normal workspace trust. */
export async function testMcpConfiguration(runtime: Runtime, name: string): Promise<string> {
  await runtime.trust(async () => false);
  const result = await runtime.harness.testMcpServer(name, { cwd: runtime.options.cwd });
  if (!result.success) throw new Error(`MCP test failed: ${result.output}`);
  return result.output;
}
