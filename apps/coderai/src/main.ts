import { Command } from 'commander';
import { fileURLToPath } from 'node:url';
import { getAsset, isSea } from './sea.js';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { configureTextBuildWorkerRuntime } from '@coderai/sdk';
import { Runtime } from './runtime.js';
import { headless } from './headless.js';
import { interactive } from './tui/app.js';
import { setup } from './setup.js';
import { latestSessionId } from './session-selection.js';
import { forkSession, exportSessionMarkdown, importSession } from './session-operations.js';
import { doctor, listProviders, setProvider, removeProvider, listModels, setModel, setDefaultModel, setDefaultPermission } from './config-commands.js';
import { listMcpConfiguration, writeMcpConfiguration, removeMcpConfiguration, testMcpConfiguration } from './mcp-operations.js';

if (isSea()) {
  const workerDir = mkdtempSync(join(tmpdir(), 'coderai-worker-'));
  const workerPath = join(workerDir, 'text-build-worker.mjs');
  writeFileSync(workerPath, Buffer.from(getAsset('text-build-worker.mjs')));
  process.on('exit', () => rmSync(workerDir, { recursive: true, force: true }));
  configureTextBuildWorkerRuntime(workerPath);
} else if ((globalThis as typeof globalThis & { __coderaiBunWorker?: string }).__coderaiBunWorker) {
  configureTextBuildWorkerRuntime((globalThis as typeof globalThis & { __coderaiBunWorker: string }).__coderaiBunWorker);
} else {
  configureTextBuildWorkerRuntime(fileURLToPath(new URL('./text-build-worker.mjs', import.meta.url)));
}
function collect(value: string, previous: string[]): string[] { return [...previous, value]; }
const program = new Command().name('coderai').description('CoderAI — a terminal coding assistant').version('0.1.0')
  .option('--setup', 'configure providers and models interactively')
  .option('-p, --prompt <text>', 'run a prompt and exit')
  .option('-C, --cwd <directory>', 'working directory', process.cwd())
  .option('-r, --resume <id>', 'resume a saved conversation')
  .option('-c, --continue', 'continue the latest conversation in this workspace')
  .option('-m, --model <alias>', 'configured model alias')
  .option('--plan', 'start in plan mode')
  .option('--agent <name>', 'select the main agent profile')
  .option('--agent-file <path>', 'load an agent profile file', collect, [])
  .option('--skills-dir <path>', 'load skills from an additional directory', collect, [])
  .option('--add-dir <path>', 'allow access to an additional directory', collect, [])
  .option('--wait-background', 'wait for background tasks in headless mode')
  .option('--attach <path>', 'attach an image or video file (headless only)', collect, [])
  .option('--json', 'emit newline-delimited JSON events (headless only)')
  .option('--yolo', 'automatically approve ordinary tool actions (explicit opt-in)')
  .option('--trust', 'trust this workspace and activate its configuration');
program.command('login', { hidden: true }).description('show API-key setup guidance').action(() => {
  process.stdout.write('CoderAI does not require an account login.\nSet CODERAI_MODEL_NAME and OPENAI_API_KEY to use an OpenAI-compatible API.\nFor a custom endpoint, also set CODERAI_MODEL_BASE_URL.\nSee README.md for other providers and config.toml examples.\n');
});
async function withRuntime(run: (runtime: Runtime) => Promise<void>): Promise<void> {
  const opts = program.opts();
  const runtime = new Runtime({ cwd: opts['cwd'], trust: opts['trust'] });
  try { await run(runtime); } finally { await runtime.close(); }
}
program.command('doctor').description('check local configuration and dependencies').action(() => withRuntime(async runtime => { process.exitCode = await doctor(runtime); }));
const providers = program.command('providers').description('manage configured providers');
providers.command('list').action(() => withRuntime(listProviders));
providers.command('add <id> <type> <base-url> <key-env>').action((id: string, type: string, url: string, key: string) => withRuntime(runtime => setProvider(runtime, id, type, url, key)));
providers.command('remove <id>').description('remove a provider and its associated models').action((id: string) => withRuntime(runtime => removeProvider(runtime, id)));
const models = program.command('models').description('manage configured model aliases');
models.command('list').action(() => withRuntime(listModels));
models.command('add <alias> <provider> <model> <context-size> [capabilities]').action((alias: string, provider: string, model: string, context: string, capabilities = '') => withRuntime(runtime => setModel(runtime, alias, provider, model, context, capabilities)));
models.command('default <alias>').action((alias: string) => withRuntime(runtime => setDefaultModel(runtime, alias)));
models.command('secondary <alias>').action((alias: string) => withRuntime(runtime => setDefaultModel(runtime, alias, true)));
program.command('permission-default <mode>').description('set default permission mode').action((mode: string) => withRuntime(runtime => setDefaultPermission(runtime, mode)));
const mcp = program.command('mcp').description('manage local MCP server configuration');
mcp.command('list').action(() => withRuntime(async runtime => { process.stdout.write(`${await listMcpConfiguration(runtime)}\n`); }));
mcp.command('add <name> <json-file>').action((name: string, path: string) => withRuntime(async runtime => { await writeMcpConfiguration(runtime, 'add', name, path); process.stdout.write(`MCP server ${name} added.\n`); }));
mcp.command('update <name> <json-file>').action((name: string, path: string) => withRuntime(async runtime => { await writeMcpConfiguration(runtime, 'update', name, path); process.stdout.write(`MCP server ${name} updated.\n`); }));
mcp.command('remove <name>').action((name: string) => withRuntime(async runtime => { await removeMcpConfiguration(runtime, name); process.stdout.write(`MCP server ${name} removed.\n`); }));
mcp.command('test <name>').action((name: string) => withRuntime(async runtime => { process.stdout.write(`${await testMcpConfiguration(runtime, name)}\n`); }));
const sessions = program.command('sessions').description('manage saved conversations');
sessions.action(async () => {
  const runtime = new Runtime({ cwd: program.opts()['cwd'] });
  try {
    for (const s of await runtime.harness.listSessions()) process.stdout.write(`${s.id}\t${s.title ?? ''}\t${s.workDir}\n`);
  } finally { await runtime.close(); }
});
sessions.command('fork <id>').description('fork a saved conversation').action(async (id: string) => {
  const opts = program.opts();
  const runtime = new Runtime({ cwd: opts['cwd'], trust: opts['trust'] });
  try { process.stdout.write(`${await forkSession(runtime, id)}\n`); } finally { await runtime.close(); }
});
sessions.command('export <id> <path>').description('export a saved conversation as Markdown').action(async (id: string, path: string) => {
  const opts = program.opts();
  const runtime = new Runtime({ cwd: opts['cwd'], trust: opts['trust'] });
  try { process.stdout.write(`${await exportSessionMarkdown(runtime, id, path)}\n`); } finally { await runtime.close(); }
});
sessions.command('import <source> <path>').description('import a local codex or claude-code session').action(async (source: string, path: string) => {
  if (source !== 'codex' && source !== 'claude-code') throw new Error('Import source must be codex or claude-code.');
  const opts = program.opts();
  const runtime = new Runtime({ cwd: opts['cwd'], model: opts['model'], trust: opts['trust'] });
  try {
    const result = await importSession(runtime, source, path);
    process.stdout.write(`${result.id}\t${result.status}\n`);
    for (const issue of result.issues) process.stderr.write(`Import: ${issue}\n`);
  } finally { await runtime.close(); }
});
program.action(async options => {
  if (options.setup) {
    if (options.prompt !== undefined || options.resume || options.continue || options.model || options.json || options.attach.length || options.waitBackground || options.plan || options.agent || options.agentFile.length || options.skillsDir.length || options.addDir.length || options.yolo) throw new Error('--setup cannot be combined with conversation options.');
    await withRuntime(setup);
    return;
  }
  if (options.continue && options.resume) throw new Error('Cannot combine --continue with --resume.');
  let piped = '';
  if (!process.stdin.isTTY) {
    const stop = () => process.stdin.destroy(Object.assign(new Error('Input cancelled'), { name: 'AbortError' }));
    process.on('SIGINT', stop); process.on('SIGTERM', stop);
    process.stdin.setEncoding('utf8');
    let bytes = 0;
    try {
      for await (const chunk of process.stdin) {
        bytes += Buffer.byteLength(chunk);
        if (bytes > 8 * 1024 * 1024) throw new Error('Piped input exceeds 8 MiB');
        piped += chunk;
      }
    } finally { process.off('SIGINT', stop); process.off('SIGTERM', stop); }
  }
  const prompt = [options.prompt, piped.trim()].filter(Boolean).join('\n\n');
  const isHeadless = options.prompt !== undefined || !process.stdin.isTTY;
  if (isHeadless && !prompt) throw new Error('A prompt or nonempty piped input is required');
  if (!isHeadless && !process.stdout.isTTY) throw new Error('Interactive mode requires terminal stdout. Use -p for redirected output.');
  if (!isHeadless && options.waitBackground) throw new Error('--wait-background requires headless input');
  if (!isHeadless && options.attach.length) throw new Error('--attach requires headless input');
  if (!isHeadless && options.json) throw new Error('--json requires headless input');
  const runtime = new Runtime({ cwd: options.cwd, model: options.model, resume: options.resume, yolo: options.yolo, trust: options.trust, plan: options.plan, agent: options.agent, agentFiles: options.agentFile, skillDirs: options.skillsDir, additionalDirs: options.addDir, attachments: options.attach });
  const forceExit = () => { process.stderr.write('Shutdown timed out\n'); process.exit(130); };
  try {
    if (options.continue) {
      runtime.options.resume = await latestSessionId(runtime.harness, options.cwd);
      if (!runtime.options.resume) process.stderr.write('No saved conversation in this workspace; starting a new one.\n');
    }
    if (isHeadless) {
      await runtime.trust(async () => false);
      process.exitCode = await headless(runtime, prompt, options.json === true, options.waitBackground === true);
    } else await interactive(runtime);
  } finally {
    const timer = setTimeout(forceExit, 8000); timer.unref();
    await runtime.close(); clearTimeout(timer);
  }
});
program.parseAsync().catch(error => { process.stderr.write(`coderai: ${error instanceof Error ? error.message : String(error)}\n`); process.exitCode = error instanceof Error && error.name === 'AbortError' ? 130 : 1; });
