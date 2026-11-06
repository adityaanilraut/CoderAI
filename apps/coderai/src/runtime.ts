import { mkdir, readFile, realpath, stat } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import lockfile from 'proper-lockfile';
import { homedir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { parse } from 'smol-toml';
import { identity } from './identity.js';
import {
  createCoderaiHarness,
  type CoderaiHarness, type Session, type Event, type ApprovalHandler, type QuestionHandler,
} from '@coderai/sdk';

export class ModelSetupRequired extends Error {}
export interface RuntimeOptions {
  cwd: string;
  interactive?: boolean;
  home?: string;
  model?: string;
  resume?: string;
  yolo?: boolean;
  trust?: boolean;
  attachments?: readonly string[];
  plan?: boolean;
  agent?: string;
  agentFiles?: string[];
  skillDirs?: string[];
  additionalDirs?: string[];
}
export function homePath(): string {
  return validateHome(process.env['CODERAI_HOME'] ?? join(homedir(), '.coderai'));
}
function canonicalPath(path: string): string {
  try { return realpathSync(path); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    const parent = dirname(path);
    if (parent === path) throw error;
    return join(canonicalPath(parent), basename(path));
  }
}
function validateHome(path: string): string {
  const home = canonicalPath(resolve(path));
  for (const brand of ['.kimi', '.kimi-code']) {
    const reserved = canonicalPath(join(homedir(), brand));
    const child = relative(reserved, home);
    if (child === '' || (!isAbsolute(child) && child !== '..' && !child.startsWith('..' + sep))) {
      throw new Error('CODERAI_HOME must be separate from existing Kimi data.');
    }
  }
  return home;
}
export function prepareEnvironment(home: string): void {
  process.env['KIMI_CODE_HOME'] = home;
  process.env['KIMI_CODE_TRUST_WORKSPACE'] = 'false';
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith('CODERAI_MODEL_') && value !== undefined) {
      process.env[key.replace('CODERAI_', 'KIMI_')] = value;
    }
  }
  // CoderAI's own model overrides default to an OpenAI-compatible API.
  // Explicit provider selections (including upstream KIMI_MODEL_*) still win.
  if (process.env['CODERAI_MODEL_NAME']?.trim() && !process.env['KIMI_MODEL_PROVIDER_TYPE']?.trim()) {
    process.env['KIMI_MODEL_PROVIDER_TYPE'] = 'openai';
  }
}

export class Runtime {
  readonly harness: CoderaiHarness;
  session?: Session;
  private unlock?: () => Promise<void>;
  private closing?: Promise<void>;
  private readonly checkedTrust = new Set<string>();
  private askTrust: (text: string, workDir: string) => Promise<boolean> = async () => false;
  constructor(readonly options: RuntimeOptions) {
    const home = validateHome(options.home ?? homePath());
    prepareEnvironment(home);
    this.harness = createCoderaiHarness({ homeDir: home, configPath: join(home, 'config.toml'), identity, uiMode: options.interactive ? 'terminal' : 'print', uiCapabilities: options.interactive ? ['update_panel', 'open_url'] : [], skillDirs: options.skillDirs?.map(path => resolve(options.cwd, path)), agentFiles: options.agentFiles?.map(path => resolve(options.cwd, path)) });
  }
  async validateModel(alias?: string): Promise<void> {
    if (/^(true|1|yes|on)$/i.test(process.env['KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT']?.trim() ?? '')) throw new Error('Background retention is not supported in this host. Set KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT=false.');
    let text: string | undefined;
    try {
      text = await readFile(this.harness.configPath, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error(`Cannot read configuration file: ${this.harness.configPath}`);
    }
    if (text !== undefined) {
      let raw;
      try { raw = parse(text); }
      catch { throw new Error(`Invalid TOML syntax in ${this.harness.configPath}. Check table names, quotes and brackets.`); }
      if (raw['telemetry'] === true) throw new Error(`Telemetry is unavailable in CoderAI. Set telemetry = false in ${this.harness.configPath}.`);
      for (const section of ['background', 'task']) {
        const settings = raw[section];
        if (settings && typeof settings === 'object' && !Array.isArray(settings) && 'keep_alive_on_exit' in settings && settings['keep_alive_on_exit'] === true) throw new Error('Background retention is not supported in this host. Set keep_alive_on_exit = false.');
      }
    }
    const diagnostics = await this.harness.getConfigDiagnostics();
    if (diagnostics.warnings.length) throw new Error(`Invalid config: ${diagnostics.warnings.join('; ')}`);
    const config = await this.harness.getConfig();
    const selected = alias ?? this.options.model ?? config.defaultModel;
    if (!selected) throw new ModelSetupRequired(`No model configured. Set CODERAI_MODEL_NAME and OPENAI_API_KEY (OpenAI-compatible API), or configure a provider and model in ${this.harness.configPath}. Set CODERAI_MODEL_BASE_URL for a custom endpoint. No account login is required.`);
    const model = config.models?.[selected];
    if (!model) throw new ModelSetupRequired(`Unknown model alias: ${selected}. Run coderai models list to see configured aliases.`);
    const provider = config.providers[model.provider];
    if (!provider) throw new ModelSetupRequired(`Unknown provider: ${model.provider}`);
    const types = ['kimi', 'openai', 'anthropic', 'openai_responses', 'google-genai', 'vertexai'];
    if (!types.includes(provider.type)) throw new Error(`Unsupported provider: ${provider.type}`);
    const standardEnv: Record<string, string> = { kimi: 'KIMI_API_KEY', openai: 'OPENAI_API_KEY', openai_responses: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY', 'google-genai': 'GOOGLE_API_KEY', vertexai: 'VERTEXAI_API_KEY' };
    const standardName = standardEnv[provider.type] ?? '';
    const key = provider.apiKey?.trim() || (provider.apiKeyEnv
      ? process.env[provider.apiKeyEnv]?.trim()
      : provider.env?.[standardName]?.trim() || process.env[standardName]?.trim());
    if (!key && !provider.oauth && provider.type !== 'vertexai') {
      throw new ModelSetupRequired(`Missing API key for provider ${model.provider}. Set ${provider.apiKeyEnv ?? standardEnv[provider.type] ?? 'CODERAI_MODEL_API_KEY'} or configure an API key in ${this.harness.configPath}.`);
    }
  }
  async trust(ask: (text: string, workDir: string) => Promise<boolean>): Promise<void> {
    this.askTrust = ask;
    let cwd = this.options.cwd;
    if (this.options.resume) {
      const saved = (await this.harness.listSessions()).find(s => s.id === this.options.resume);
      if (!saved) throw new Error(`Session not found: ${this.options.resume}`);
      cwd = saved.workDir;
    }
    await this.trustDirectory(cwd);
  }
  private async trustDirectory(cwd: string): Promise<void> {
    const path = resolve(cwd);
    let workDir: string;
    try {
      workDir = await realpath(path);
      if (!(await stat(workDir)).isDirectory()) throw new Error(`Workspace path is not a directory: ${path}`);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'ENOENT' || code === 'ENOTDIR') throw new Error(`Workspace directory does not exist: ${path}`);
      if (code === 'EACCES' || code === 'EPERM') throw new Error(`Cannot access workspace directory: ${path}`);
      throw error;
    }
    if (this.checkedTrust.has(workDir)) return;
    const info = await this.harness.getWorkspaceTrustInfo(workDir);
    if (info.trusted) { this.checkedTrust.add(workDir); return; }
    const lines = [`Trust ${workDir}?`, 'Project instructions, hooks, plugins and configured MCP programs may run in a trusted workspace.'];
    const add = (label: string, values: readonly string[]) => { if (values.length) lines.push(`${label}:\n${values.map(value => `  ${value}`).join('\n')}`); };
    add('MCP servers', info.gatedMcpServers.map(server => `${server.name} (${server.transport}) ${server.command ?? server.url ?? ''} ${server.args?.join(' ') ?? ''} — ${server.origin}`));
    add('Additional directories', info.gatedAdditionalDirs);
    add('Additional directory sources', info.additionalDirSources);
    add('Instructions', info.instructionSources.agentsMdPaths);
    add('Skills', info.instructionSources.skills);
    add('Agent profiles', info.instructionSources.agentProfiles);
    add('Configuration sources', info.instructionSources.paths);
    add('Warnings', info.warnings);
    const details = lines.join('\n\n');
    if (!this.options.trust && !await this.askTrust(details, workDir)) {
      throw new Error(`Workspace is not trusted: ${workDir}. Trust it interactively or use --trust to proceed.`);
    }
    await this.harness.trustWorkspace(workDir);
    this.checkedTrust.add(workDir);
  }
  async open(id = this.options.resume): Promise<Session> {
    if (this.closing) throw new Error('CoderAI is shutting down');
    if (id && this.session?.id === id && !this.session.isClosed) return this.session;
    let unlock: (() => Promise<void>) | undefined;
    if (id) {
      const saved = (await this.harness.listSessions()).find(s => s.id === id);
      if (!saved) throw new Error(`Session not found: ${id}`);
      await this.trustDirectory(saved.workDir);
      unlock = await sessionLock(this.harness.homeDir, id);
    } else {
      await this.trustDirectory(this.options.cwd);
    }
    let session: Session | undefined;
    try {
      // A resumed session may have a valid saved model even if the current
      // default is invalid. Validate that binding after restoring it.
      if (!id || this.options.model) await this.validateModel();
      if (id && this.options.agentFiles?.length) throw new Error('--agent-file can only be used when creating a session.');
      const additionalDirs = this.options.additionalDirs?.length ? await Promise.all(this.options.additionalDirs.map(path => realpath(resolve(this.options.cwd, path)))) : undefined;
      session = id
        ? await this.harness.resumeSession({ id, additionalDirs, agentProfile: this.options.agent })
        : await this.harness.createSession({ workDir: await realpath(resolve(this.options.cwd)), model: this.options.model, permission: this.options.yolo ? 'yolo' : undefined, planMode: this.options.plan, agentProfile: this.options.agent, agentFiles: this.options.agentFiles?.map(path => resolve(this.options.cwd, path)), additionalDirs });
      if (id && this.options.plan) await session.setPlanMode(true);
      if (!id) unlock = await sessionLock(this.harness.homeDir, session.id);
      // Persist the effective mode even when it is the engine's implicit
      // manual default, so a later config change cannot silently change an
      // existing session's approvals on resume.
      if (!id) await session.setPermission((await session.getStatus()).permission);
      else if (this.options.yolo) await session.setPermission('yolo');
      if (id && this.options.model) await session.setModel(this.options.model);
      if (id && !this.options.model) await this.validateModel((await session.getStatus()).model);
      await this.session?.close();
      await this.unlock?.();
      this.session = session;
      this.unlock = unlock;
      return session;
    } catch (error) {
      try { await session?.close(); }
      finally { await unlock?.(); }
      throw error;
    }
  }
  async run(prompt: string, onEvent: (event: Event) => void, approve: ApprovalHandler, question?: QuestionHandler, dispatch?: (session: Session) => Promise<void>, followGoal = false, followCreatedGoals = false, printCompletion = false, signal?: AbortSignal): Promise<string> {
    const session = this.session;
    if (!session) throw new Error('No active conversation');
    session.setApprovalHandler(approve);
    session.setQuestionHandler(question ?? (() => null));
    const config = printCompletion ? await this.harness.getConfig() : undefined;
    const configuredCeiling = config?.task?.['printWaitCeilingS'] ?? config?.background?.printWaitCeilingS;
    const ceiling = typeof configuredCeiling === 'number' && Number.isFinite(configuredCeiling) && configuredCeiling > 0 ? configuredCeiling : 300;
    return new Promise<string>((resolveTurn, reject) => {
      let activeTurn = false;
      let trackGoal = followGoal;
      let endedReason = 'completed';
      let settled = false;
      let completionTimer: ReturnType<typeof setTimeout> | undefined;
      let sequence = 0;
      let completionSequence = -1;
      const abort = () => finish('cancelled');
      const cleanup = () => { off(); if (completionTimer) clearTimeout(completionTimer); signal?.removeEventListener('abort', abort); };
      const finish = (reason: string) => { if (!settled) { settled = true; cleanup(); resolveTurn(reason); } };
      const fail = (error: unknown) => { if (!settled) { settled = true; cleanup(); reject(error); } };
      const complete = (reason: string) => {
        if (!printCompletion || reason !== 'completed') { finish(reason); return; }
        const current = sequence;
        if (completionSequence === current) return;
        completionSequence = current;
        void session.handlePrintMainTurnCompleted().then(policy => {
          if (settled || current !== sequence) return;
          if (policy === 'finish') finish(reason);
          else if (!completionTimer) completionTimer = setTimeout(() => finish('background_timeout'), Math.min(ceiling * 1000, 2_147_483_647));
        }).catch(fail);
      };
      const off = session.onEvent(event => {
        onEvent(event);
        if (event.agentId !== 'main') return;
        if (event.type === 'turn.started') { activeTurn = true; sequence++; }
        if (event.type === 'goal.updated' && followCreatedGoals && event.snapshot?.status === 'active') trackGoal = true;
        if (event.type === 'goal.updated' && trackGoal && !activeTurn && event.snapshot?.status !== 'active') complete(endedReason);
        if (event.type === 'turn.ended') {
          activeTurn = false; endedReason = event.reason;
          if (!trackGoal || event.reason !== 'completed') { complete(event.reason); return; }
          void session.getGoal().then(({ goal }) => { if (!activeTurn && goal?.status !== 'active') complete(event.reason); }).catch(fail);
        }
      });
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) { abort(); return; }
      (dispatch ? dispatch(session) : session.prompt(prompt)).catch(fail);
    });
  }
  close(): Promise<void> {
    return this.closing ??= (async () => {
      try {
        try { if (this.session && !this.session.isClosed) await this.session.cancel(); }
        finally { await this.session?.close(); }
      } finally {
        try { await this.harness.close(); }
        finally { await this.unlock?.(); }
      }
    })();
  }
}

async function sessionLock(home: string, id: string): Promise<() => Promise<void>> {
  if (!/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error('Invalid session ID');
  const dir = join(home, 'locks'); await mkdir(dir, { recursive: true, mode: 0o700 });
  const path = join(dir, `${id}.lock`);
  try {
    return await lockfile.lock(path, { realpath: false, retries: 0, stale: 10000, update: 2000 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ELOCKED') throw new Error(`Session ${id} is in use by another process`);
    throw error;
  }
}
