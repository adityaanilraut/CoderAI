import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { McpServerConfigSchema } from '@coderai/sdk/mcp-config';
import type { McpServerConfig } from '@coderai/sdk';
import type { Runtime } from './runtime.js';
import { browserUrl, openBrowser } from './browser.js';

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
  const server = await readMcpConfiguration(name, path, runtime.options.cwd);
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

export async function authenticateMcpConfiguration(runtime: Runtime, name: string, options: {
  signal?: AbortSignal;
  open?: (url: string) => Promise<boolean>;
  onUrl?: (url: string) => void;
} = {}): Promise<void> {
  if (!runtime.session) await runtime.trust(async () => false);
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.on('SIGINT', abort); process.on('SIGTERM', abort);
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) abort();
  try {
    controller.signal.throwIfAborted();
    await runtime.harness.authenticateMcpServer(name, {
      cwd: runtime.session?.workDir ?? runtime.options.cwd,
      signal: controller.signal,
      timeoutMs: 180_000,
      onAuthorizationUrl: async value => {
        controller.signal.throwIfAborted();
        const url = browserUrl(value);
        (options.onUrl ?? (url => process.stdout.write(`Open this URL to authorize ${name}:\n${url}\n`)))(url);
        await (options.open ?? openBrowser)(url);
        return !controller.signal.aborted;
      },
    });
    if (runtime.session) await runtime.session.reconnectMcpServer(name);
  } finally {
    process.off('SIGINT', abort); process.off('SIGTERM', abort);
    options.signal?.removeEventListener('abort', abort);
  }
}

export async function resetMcpAuthentication(runtime: Runtime, name: string): Promise<void> {
  if (!runtime.session) await runtime.trust(async () => false);
  await runtime.harness.resetMcpServerAuth(name, { cwd: runtime.session?.workDir ?? runtime.options.cwd });
  if (runtime.session) await runtime.session.reconnectMcpServer(name);
}

export async function readMcpConfiguration(name: string, path: string, cwd = process.cwd()): Promise<McpServerConfig> {
  if (!name.trim()) throw new Error('MCP server name cannot be empty.');
  let input: unknown;
  try { input = JSON.parse(await readFile(resolve(cwd, path), 'utf8')); }
  catch (error) { throw new Error(`Cannot read MCP configuration JSON: ${error instanceof Error ? error.message : String(error)}`); }
  const parsed = McpServerConfigSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Invalid MCP server configuration: ${parsed.error.issues.map(issue => `${issue.path.join('.') || 'config'}: ${issue.message}`).join('; ')}`);
  return { name: name.trim(), ...parsed.data };
}
