import { capabilityText, installCapabilityAndWait, resolvePluginSource, pluginAction } from './plugin-operations.js';
import { Command } from 'commander';
import { fileURLToPath } from 'node:url';
import { configureTextBuildWorkerRuntime } from '@coderai/sdk';
import { identity } from './identity.js';
import { Runtime } from './runtime.js';
import { runImportSessionsCommand } from './session-import/command.js';
import { headless } from './headless.js';
import { interactive } from './tui/app.js';
import { setup } from './setup.js';
import { sanitizeTerminalText } from './tui/sanitize.js';
import { latestSessionId } from './session-selection.js';
import { forkSession, exportSessionMarkdown, exportSessionZip, importSession, listSessions, parseLimit, parseTurn } from './session-operations.js';
import { doctor, importProviders, refreshProviders, listProviders, setProvider, removeProvider, listModels, setModel, setDefaultModel, setDefaultPermission } from './config-commands.js';
import { authenticateMcpConfiguration, resetMcpAuthentication, listMcpConfiguration, writeMcpConfiguration, removeMcpConfiguration, testMcpConfiguration } from './mcp-operations.js';

function collect(value: string, previous: string[]): string[] { return [...previous, value]; }
const program = new Command().name('coderai').description('CoderAI — a terminal coding assistant').version(identity.version)
  .configureHelp({ helpWidth: 100 })
  .configureOutput({ writeErr: message => process.stderr.write(sanitizeTerminalText(message)) })
  .addHelpText('after', '\nExamples:\n  coderai --setup\n  coderai -C ./project\n  coderai -p "Explain this project"\n  coderai sessions list --json\n\nRun coderai <command> --help for command options.\n')
  .option('--setup', 'configure providers and models interactively')
  .option('-p, --prompt <text>', 'run a prompt and exit')
  .option('-C, --cwd <directory>', 'working directory (defaults to the current directory)')
  .option('-r, --resume <id>', 'resume a saved conversation')
  .option('-c, --continue', 'continue the latest conversation in this workspace')
  .option('-m, --model <alias>', 'configured model alias (defaults to the saved model)')
  .option('--plan', 'start in plan mode')
  .option('--agent <name>', 'select the main agent profile')
  .option('--agent-file <path>', 'load an agent profile file for a new conversation', collect, [])
  .option('--skills-dir <path>', 'load skills from another directory (repeatable)', collect, [])
  .option('--add-dir <path>', 'allow access to another directory (repeatable)', collect, [])
  .option('--wait-background', 'wait for background tasks in headless mode')
  .option('--attach <path>', 'stage an image, video or audio file for the next prompt', collect, [])
  .option('--json', 'emit newline-delimited JSON events (headless only)')
  .option('--yolo', 'automatically approve ordinary tool actions (explicit opt-in)')
  .option('--trust', 'trust this workspace and activate its configuration');
program.command('login', { hidden: true }).description('show API-key setup guidance').action(() => {
  process.stdout.write('CoderAI does not require an account login.\nSet CODERAI_MODEL_NAME and OPENAI_API_KEY to use an OpenAI-compatible API.\nFor a custom endpoint, also set CODERAI_MODEL_BASE_URL.\nSee README.md for other providers and config.toml examples.\n');
});
async function withRuntime(run: (runtime: Runtime) => Promise<void>, useModelOverride = false): Promise<void> {
  const opts = program.opts();
  const runtime = new Runtime({ cwd: opts['cwd'] ?? process.cwd(), trust: opts['trust'], model: useModelOverride ? opts['model'] : undefined });
  try { await run(runtime); } finally { await runtime.close(); }
}
program.command('doctor').description('check local configuration and dependencies').action(() => withRuntime(async runtime => { process.exitCode = await doctor(runtime); }));
const providers = program.command('providers').description('manage configured providers');
providers.command('list').description('list configured providers').action(() => withRuntime(listProviders));
providers.command('add <id> <type> <base-url> <key-env>').description('add a provider using an API key environment variable').action((id: string, type: string, url: string, key: string) => withRuntime(runtime => setProvider(runtime, id, type, url, key)));
providers.command('update <id> <type> <base-url> <key-env>').description('update a provider while preserving its model aliases').action((id: string, type: string, url: string, key: string) => withRuntime(runtime => setProvider(runtime, id, type, url, key, undefined, 'update')));
providers.command('import <url> [key-env]').description('import or replace providers from a Kimi-compatible api.json registry').action((url: string, keyEnv?: string) => withRuntime(runtime => importProviders(runtime, url, keyEnv)));
providers.command('refresh').description('refresh models from configured provider registries').action(() => withRuntime(refreshProviders));
providers.command('remove <id>').description('remove a provider and its associated models').action((id: string) => withRuntime(runtime => removeProvider(runtime, id)));
const models = program.command('models').description('manage configured model aliases');
models.command('list').description('list model aliases and their providers').action(() => withRuntime(listModels));
models.command('add <alias> <provider> <model> <context-size> [capabilities]').description('add a model alias with an optional comma-separated capability list').action((alias: string, provider: string, model: string, context: string, capabilities = '') => withRuntime(runtime => setModel(runtime, alias, provider, model, context, capabilities)));
models.command('update <alias> <provider> <model> <context-size> [capabilities]').description('update a model alias; omitted capabilities keep their current value').action((alias: string, provider: string, model: string, context: string, capabilities?: string) => withRuntime(async runtime => {
  const config = await runtime.harness.getConfig();
  await setModel(runtime, alias, provider, model, context, capabilities ?? config.models?.[alias]?.capabilities?.join(',') ?? '', 'update');
}));
models.command('default <alias>').description('set the default model for new conversations').action((alias: string) => withRuntime(runtime => setDefaultModel(runtime, alias)));
models.command('secondary <alias>').description('set the default model for new subagents').action((alias: string) => withRuntime(runtime => setDefaultModel(runtime, alias, true)));
program.command('permission-default <mode>').description('set default permission mode').action((mode: string) => withRuntime(runtime => setDefaultPermission(runtime, mode)));
const mcp = program.command('mcp').description('manage local MCP server configuration');
mcp.command('list').action(() => withRuntime(async runtime => { process.stdout.write(`${await listMcpConfiguration(runtime)}\n`); }));
mcp.command('add <name> <json-file>').action((name: string, path: string) => withRuntime(async runtime => { await writeMcpConfiguration(runtime, 'add', name, path); process.stdout.write(`MCP server ${name} added.\n`); }));
mcp.command('update <name> <json-file>').action((name: string, path: string) => withRuntime(async runtime => { await writeMcpConfiguration(runtime, 'update', name, path); process.stdout.write(`MCP server ${name} updated.\n`); }));
mcp.command('remove <name>').action((name: string) => withRuntime(async runtime => { await removeMcpConfiguration(runtime, name); process.stdout.write(`MCP server ${name} removed.\n`); }));
mcp.command('test <name>').action((name: string) => withRuntime(async runtime => { process.stdout.write(`${await testMcpConfiguration(runtime, name)}\n`); }));
mcp.command('auth <name>').description('authorize an MCP server in your browser').option('--no-browser', 'print the authorization URL without opening a browser').action((name: string, options: { browser: boolean }) => withRuntime(async runtime => {
  await authenticateMcpConfiguration(runtime, name, { open: options.browser ? undefined : async () => false });
  process.stdout.write(`MCP server ${name} authorized.\n`);
}));
mcp.command('logout <name>').description('remove saved MCP authorization').action((name: string) => withRuntime(async runtime => { await resetMcpAuthentication(runtime, name); process.stdout.write(`MCP authorization removed: ${name}\n`); }));
const plugins = program.command('plugins').description('manage local plugins and capabilities');
plugins.action(() => withRuntime(async runtime => { process.stdout.write(await pluginAction(runtime.harness, 'list', []) + '\n'); }));
for (const [spec, action] of [
  ['list', 'list'], ['info <id>', 'info'], ['install <source>', 'install'],
  ['enable <id>', 'enable'], ['disable <id>', 'disable'], ['remove <id>', 'remove'], ['reload', 'reload'],
] as const) plugins.command(spec).action((...values: unknown[]) => withRuntime(async runtime => {
  const args = values.filter((value): value is string => typeof value === 'string');
  if (action === 'install') args[0] = resolvePluginSource(args[0]!, runtime.options.cwd);
  process.stdout.write(await pluginAction(runtime.harness, action, args) + '\n');
}));
plugins.command('mcp <action> <id> <server>').description('enable or disable a plugin MCP server')
  .action((action: string, id: string, server: string) => withRuntime(async runtime => { process.stdout.write(await pluginAction(runtime.harness, 'mcp', [action, id, server]) + '\n'); }));
plugins.command('capabilities [action] [id]').description('list, inspect or install a capability')
  .action((action?: string, id?: string) => withRuntime(async runtime => {
    if (action === 'install' && id) {
      const abort = new AbortController(); const cancel = () => abort.abort();
      process.on('SIGINT', cancel); process.on('SIGTERM', cancel);
      try { process.stdout.write(capabilityText(await installCapabilityAndWait(runtime.harness, id, { signal: abort.signal })) + '\n'); }
      finally { process.off('SIGINT', cancel); process.off('SIGTERM', cancel); }
    } else process.stdout.write(await pluginAction(runtime.harness, 'capabilities', [action, id].filter((value): value is string => value !== undefined)) + '\n');
  }));
program.command('skills').description('inspect available skills').command('list').action(() => withRuntime(async runtime => {
  if (runtime.options.trust) await runtime.trust(async () => false);
  const skills = await runtime.harness.listWorkspaceSkills(runtime.options.cwd);
  process.stdout.write((skills.map(skill => `${skill.name} (${skill.source})${skill.description ? ` — ${skill.description}` : ''}`).join('\n') || 'No skills available.') + '\n');
}));
program.command('import-sessions').description('import recent Claude Code and Codex sessions').action(() => withRuntime(runImportSessionsCommand));
const sessions = program.command('sessions').description('manage saved conversations');
function listOptions(cmd: Command): Command {
  return cmd.option('--all', 'list across every workspace').option('--archived', 'include archived conversations')
    .option('--limit <n>', 'show at most n conversations', parseLimit).option('--json', 'emit session summaries as JSON');
}
const printSessions = (options: { all?: boolean; archived?: boolean; limit?: number; json?: boolean }) => withRuntime(async runtime => { process.stdout.write(`${await listSessions(runtime, { ...options, json: options.json ?? program.opts()['json'] })}\n`); });
listOptions(sessions).action(printSessions);
listOptions(sessions.command('list').description('list saved conversations, newest first')).action(options => printSessions({ ...sessions.opts(), ...options }));
sessions.command('delete <id>').description('delete a saved conversation').action((id: string) => withRuntime(async runtime => {
  await runtime.harness.deleteSession(id); process.stdout.write(`Deleted ${id}\n`);
}));
sessions.command('rename <id> <title>').description('rename a saved conversation').action((id: string, title: string) => withRuntime(async runtime => {
  if (!title.trim()) throw new Error('Title must not be empty.');
  await runtime.harness.renameSession({ id, title: title.trim() }); process.stdout.write(`Renamed ${id}\n`);
}));
sessions.command('fork <id>').description('fork a saved conversation')
  .option('--turn <n>', 'retain through this zero-based turn', parseTurn).option('--title <title>', 'title for the fork')
  .action((id: string, options: { turn?: number; title?: string }) => withRuntime(async runtime => {
    process.stdout.write(`${await forkSession(runtime, id, { turnIndex: options.turn, title: options.title })}\n`);
  }));
sessions.command('export <id> [path]').description('export a saved conversation as Markdown or a ZIP archive')
  .option('--zip', 'export a diagnostic ZIP archive')
  .action((id: string, path: string | undefined, options: { zip?: boolean }) => withRuntime(async runtime => {
    if (!options.zip && !path) throw new Error('A Markdown output path is required. Use --zip for an archive.');
    process.stdout.write(`${options.zip ? await exportSessionZip(runtime, id, path) : await exportSessionMarkdown(runtime, id, path!)}\n`);
  }));
sessions.command('import <source> <path>').description('import a local codex or claude-code session').action((source: string, path: string) => withRuntime(async runtime => {
  if (source !== 'codex' && source !== 'claude-code') throw new Error('Import source must be codex or claude-code.');
  const result = await importSession(runtime, source, path);
  process.stdout.write(`${result.id}\t${result.status}\n`);
  for (const issue of result.issues) process.stderr.write(`Import: ${sanitizeTerminalText(issue)}\n`);
}, true));
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
  if (isHeadless && !prompt.trim()) throw new Error('A prompt or nonempty piped input is required. Use -p TEXT or pipe text into coderai.');
  if (!isHeadless && !process.stdout.isTTY) throw new Error('Interactive mode requires terminal stdout. Use -p for redirected output.');
  if (!isHeadless && options.waitBackground) throw new Error('--wait-background requires headless input');
  if (!isHeadless && options.json) throw new Error('--json requires headless input');
  const runtime = new Runtime({ interactive: !isHeadless, cwd: options.cwd ?? process.cwd(), model: options.model, resume: options.resume, yolo: options.yolo, trust: options.trust, plan: options.plan, agent: options.agent, agentFiles: options.agentFile, skillDirs: options.skillsDir, additionalDirs: options.addDir, attachments: options.attach });
  const forceExit = () => { process.stderr.write('Shutdown timed out\n'); process.exit(130); };
  try {
    if (options.continue) {
      runtime.options.resume = await latestSessionId(runtime.harness, runtime.options.cwd);
      if (!runtime.options.resume) process.stderr.write('No saved conversation in this workspace; starting a new one.\n');
    }
    if (isHeadless) {
      await runtime.trust(async () => false);
      process.exitCode = await headless(runtime, prompt, options.json === true, options.waitBackground === true);
    } else await interactive(runtime);
  } finally {
    const timer = setTimeout(forceExit, 8000); timer.unref();
    try { await runtime.close(); }
    finally { clearTimeout(timer); }
  }
});
async function main(): Promise<void> {
  const standaloneWorker = (globalThis as typeof globalThis & { __coderaiBunWorker?: string }).__coderaiBunWorker;
  configureTextBuildWorkerRuntime(standaloneWorker ?? fileURLToPath(new URL('./text-build-worker.mjs', import.meta.url)));
  await program.parseAsync();
}

void main().catch(error => {
  const cancelled = error instanceof Error && error.name === 'AbortError';
  process.exitCode = cancelled ? 130 : 1;
  const message = cancelled ? 'Cancelled.' : error instanceof Error ? error.message : String(error);
  process.stderr.write(`coderai: ${sanitizeTerminalText(message)}\n`);
});
