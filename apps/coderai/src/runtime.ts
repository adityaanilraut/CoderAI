import { mkdir, readFile, realpath, stat } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import lockfile from 'proper-lockfile';
import { homedir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { parse } from 'smol-toml';
import { identity } from './identity.js';
import {
  createCoderaiHarness,
  type CoderaiHarness, type Session, type Event, type ApprovalHandler, type QuestionHandler, type GoalSnapshot,
} from '@coderai/sdk';

export class ModelSetupRequired extends Error {}
type PromptDispatch = (session: Session, promptId: string, signal: AbortSignal) => Promise<void>;
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
  private pendingRun?: { cancel: () => Promise<void> };
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
    if (!config.models || !Object.hasOwn(config.models, selected)) {
      throw new ModelSetupRequired(`Unknown model alias: ${selected}. Run coderai models list to see configured aliases.`);
    }
    try {
      await this.harness.ensureModelReady(selected);
    } catch (error) {
      throw new ModelSetupRequired(error instanceof Error ? error.message : String(error));
    }
  }
  async trust(ask: (text: string, workDir: string) => Promise<boolean>): Promise<void> {
    this.askTrust = ask;
    let cwd = this.options.cwd;
    if (this.options.resume) {
      const [saved] = await this.harness.listSessions({ sessionId: this.options.resume, includeArchived: true });
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
    add('MCP servers', info.gatedMcpServers.map(server => {
      const args = server.argumentCount ? ` (${server.argumentCount} hidden arguments)` : '';
      const environment = server.environmentNames?.length ? ` · environment: ${server.environmentNames.join(', ')}` : '';
      return `${server.name} (${server.transport}) ${server.command ?? server.url ?? ''}${args}${environment} — ${server.origin}`;
    }));
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
      const [saved] = await this.harness.listSessions({ sessionId: id, includeArchived: true });
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
  async cancelPending(): Promise<void> {
    if (this.pendingRun) await this.pendingRun.cancel();
    else await this.session?.cancel();
  }

  async run(prompt: string, onEvent: (event: Event, owned: boolean) => void, approve: ApprovalHandler, question?: QuestionHandler, dispatch?: PromptDispatch, followGoal = false, followCreatedGoals = false, printCompletion: boolean | 'drain' = false, signal?: AbortSignal): Promise<string> {
    const session = this.session;
    if (!session) throw new Error('No active conversation');
    if (this.pendingRun) throw new Error('Another prompt is being submitted in this conversation');
    const promptId = randomUUID();
    const controller = new AbortController();
    const pending = { cancel: async () => { controller.abort(); } };
    const abort = () => { void pending.cancel().catch(() => undefined); };
    this.pendingRun = pending;
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    try {
      return await this.runPrompt(session, prompt, promptId, onEvent, approve, question, dispatch, followGoal, followCreatedGoals, printCompletion, controller, pending);
    } finally {
      signal?.removeEventListener('abort', abort);
      this.pendingRun = undefined;
    }
  }

  private async runPrompt(
    session: Session, prompt: string, promptId: string, onEvent: (event: Event, owned: boolean) => void,
    approve: ApprovalHandler, question: QuestionHandler | undefined, dispatch: PromptDispatch | undefined,
    followGoal: boolean, followCreatedGoals: boolean, printCompletion: boolean | 'drain',
    controller: AbortController, pending: { cancel: () => Promise<void> },
  ): Promise<string> {
    session.setApprovalHandler(approve);
    session.setQuestionHandler(question ?? (() => null));
    const [config, initialGoal] = await Promise.all([
      printCompletion ? this.harness.getConfig() : undefined,
      followGoal ? session.getGoal().then(({ goal }) => goal) : undefined,
    ]);
    if (controller.signal.aborted) return 'cancelled';
    const printMode = config?.task?.['printBackgroundMode'] ?? config?.background?.printBackgroundMode ?? 'steer';
    const configuredCeiling = config?.task?.['printWaitCeilingS'] ?? config?.background?.printWaitCeilingS;
    const ceiling = typeof configuredCeiling === 'number' && Number.isFinite(configuredCeiling) && configuredCeiling > 0 ? configuredCeiling : 300;
    return new Promise<string>((resolveTurn, reject) => {
      let activeTurn: number | undefined;
      let primaryTurn: number | undefined;
      let goal: GoalSnapshot | null | undefined = initialGoal;
      let lastReason: string | undefined;
      const taskIds = new Set<string>();
      let settled = false;
      let completionTimer: ReturnType<typeof setTimeout> | undefined;
      let checkingBackground = false;
      let sequence = 0;
      let completionSequence = -1;
      const abort = () => finish('cancelled');
      pending.cancel = async () => {
        const turnId = activeTurn;
        controller.abort();
        if (turnId !== undefined && turnId !== primaryTurn) await session.cancel({ turnId });
      };
      const cleanup = () => {
        off();
        if (completionTimer) clearTimeout(completionTimer);
        controller.signal.removeEventListener('abort', abort);
      };
      const finish = (reason: string) => { if (!settled) { settled = true; cleanup(); resolveTurn(reason); } };
      const fail = (error: unknown) => { if (!settled) { settled = true; cleanup(); reject(error); } };
      const complete = (reason: string) => {
        if (!printCompletion || reason !== 'completed') { finish(reason); return; }
        const current = sequence;
        if (completionSequence === current) return;
        completionSequence = current;
        checkingBackground = printCompletion !== 'drain' && printMode === 'steer';
        void session.handlePrintMainTurnCompleted({ signal: controller.signal, taskIds: [...taskIds], forceDrain: printCompletion === 'drain' }).then(policy => {
          if (settled || current !== sequence) return;
          if (policy === 'finish') finish(reason);
          else if (policy === 'timeout') finish('background_timeout');
          else if (!completionTimer) completionTimer = setTimeout(() => finish('background_timeout'), Math.min(ceiling * 1000, 2_147_483_647));
        }).catch(error => { if (current === sequence) fail(error); });
      };
      const handleEvent = (event: Event) => {
        if (event.type === 'background.task.started' && activeTurn !== undefined) taskIds.add(event.info.taskId);
        if (event.type === 'background.task.terminated') { onEvent(event, taskIds.has(event.info.taskId)); return; }
        if (event.agentId !== 'main') { onEvent(event, false); return; }
        if (event.type === 'turn.started') {
          const origin = event.origin;
          const isPrimary = event.promptId === promptId;
          const isGoalContinuation = goal?.status === 'active' && primaryTurn !== undefined &&
            origin?.kind === 'system_trigger' && origin.name === 'goal_continuation' && origin.goalId === goal.goalId;
          const isBackgroundContinuation = checkingBackground && origin?.kind === 'task' && taskIds.has(origin.taskId);
          if (isPrimary || isGoalContinuation || isBackgroundContinuation) {
            if (isPrimary) primaryTurn = event.turnId;
            activeTurn = event.turnId;
            if (completionTimer !== undefined) clearTimeout(completionTimer);
            completionTimer = undefined;
            checkingBackground = false;
            sequence++;
          }
        }
        if (event.type === 'goal.updated') {
          if (!goal && followCreatedGoals && activeTurn !== undefined && event.snapshot?.status === 'active') goal = event.snapshot;
          const owned = goal != null && (event.snapshot?.goalId ?? event.goalId) === goal.goalId;
          onEvent(event, owned);
          if (!owned) return;
          if (event.snapshot !== null) goal = event.snapshot;
          else if (goal?.status === 'active') goal = null;
          if (activeTurn === undefined && lastReason !== undefined && goal?.status !== 'active') complete(lastReason);
          return;
        }
        const isPromptOutcome = (event.type === 'prompt.completed' || event.type === 'prompt.aborted') && event.promptId === promptId;
        const owned = isPromptOutcome || (activeTurn !== undefined && (!('turnId' in event) || event.turnId === activeTurn));
        onEvent(event, owned);
        if (isPromptOutcome && primaryTurn === undefined) {
          const reason = event.type === 'prompt.aborted' ? 'cancelled' : event.reason ?? 'failed';
          if (goal?.status === 'active') {
            void session.pauseGoal().catch(() => undefined).finally(() => finish(reason));
          } else finish(reason);
          return;
        }
        if (event.type !== 'turn.ended' || !owned) return;
        activeTurn = undefined;
        const cappedGoal = goal?.status === 'active' && event.reason === 'failed' &&
          (event.interruptReason === 'max_steps' || event.error?.code === 'loop.max_steps_exceeded');
        lastReason = cappedGoal ? 'completed' : event.reason;
        if (goal?.status !== 'active' || (!cappedGoal && event.reason !== 'completed')) complete(lastReason);
      };
      const off = session.onEvent(event => {
        try { handleEvent(event); } catch (error) { fail(error); }
      });
      controller.signal.addEventListener('abort', abort, { once: true });
      if (controller.signal.aborted) { abort(); return; }
      void Promise.resolve().then(() => {
        controller.signal.throwIfAborted();
        return dispatch ? dispatch(session, promptId, controller.signal) : session.prompt(prompt, { promptId, signal: controller.signal });
      }).catch(fail);
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

export async function sessionLock(home: string, id: string): Promise<() => Promise<void>> {
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
