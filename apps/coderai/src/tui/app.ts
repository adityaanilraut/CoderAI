import { inlineSkillActivations } from '../inline-skills.js';
import { startSwarm, swarmTask, TOWER_TEARDOWN_PROMPT } from './modes.js';
import { ModelSetupRequired, type Runtime } from '../runtime.js';
import { TerminalView } from './view.js';
import { command, replay, help } from './commands.js';
import { randomUUID } from 'node:crypto';
import { describeGoal, describeMcp, describeTask, describeToolInput } from './display.js';
import type { PromptInput, Session } from '@coderai/sdk';
import { basename } from 'node:path';
import { join } from 'node:path';
import { copyFile, mkdtemp, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { appendGoalQueueItem, readGoalQueue, removeGoalQueueItem, moveGoalQueueItem, updateGoalQueueItem } from './goal-queue-store.js';
import { attachmentPrompt } from '../attachments.js';
import { readClipboardMedia } from '../clipboard-media.js';
import { mediaKindForPath } from '@coderai/sdk/media';
import { TurnActivity } from './activity.js';
import { todoListFromResult } from './todos.js';
import { renderEventNotice, McpAuthorizationOpener } from '../event-format.js';
import { openBrowser } from '../browser.js';
import { BtwPanelController } from './btw-panel.js';
import { loadTuiConfig, DEFAULT_TUI_CONFIG, saveAutoSessionTitle } from './config.js';
import { AutoSessionTitle } from './session-title.js';

const commandAliases: Record<string, string> = { '/h': '/help', '/?': '/help', '/yes': '/yolo', '/thinking': '/effort', '/task': '/tasks', '/export': '/export-md', '/q': '/quit', '/auto': '/permissions auto' };

export async function interactive(runtime: Runtime): Promise<void> {
  const commands = [...Object.entries(commandAliases).map(([name, target]) => ({ name: name.slice(1), description: `Alias for ${target}` })), ...help.split('\n').map(line => {
    const match = line.match(/^\/(\S+)\s+(.*)$/);
    return { name: match?.[1] ?? '', description: match?.[2] ?? '' };
  }).filter(item => item.name)];
  const preferenceWarnings: string[] = [];
  let initialPreferences = structuredClone(DEFAULT_TUI_CONFIG);
  try { initialPreferences = await loadTuiConfig(runtime.harness.homeDir, message => preferenceWarnings.push(message)); }
  catch (error) { preferenceWarnings.push(`TUI preferences could not be loaded: ${String(error)}`); }
  const view = new TerminalView(runtime.harness.homeDir, runtime.options.cwd, commands, initialPreferences);
  view.suggestFiles = (root, query) => runtime.harness.suggestFiles(root, { query, limit: 100 });
  for (const warning of preferenceWarnings) view.notice(warning);
  const activity = new TurnActivity(view);
  let busy = true;
  let stopping = false;
  let shellId: string | undefined;
  let editing = false;
  let observingTurn = false;
  let pendingPrompt: string | undefined;
  let runningGoal = false;
  let autoTitleEnabled = true;
  view.onPreferencesChanged = config => { autoTitleEnabled = config.autoSessionTitle; };
  try { await view.applyPreferences(initialPreferences); }
  catch (error) { view.notice(`TUI preferences could not be loaded: ${String(error)}`); }
  const titles = new AutoSessionTitle(runtime.harness, () => autoTitleEnabled && !stopping, (id, title) => {
    if (!stopping && runtime.session?.id === id) view.setSessionTitle(title);
  });
  const btw = new BtwPanelController(runtime.harness, panel => {
    view.btwPane.clear(); if (panel) view.btwPane.addChild(panel);
  }, () => view.tui.requestRender(), () => view.tui.terminal.rows, error => { if (!stopping) view.error(error); });
  view.onSessionReloaded = () => { btw.reset(); view.agents.clear(); view.updates.clear(); oauth.clear(); view.setSessionTitle(runtime.session?.summary?.title ?? ''); };
  view.onPanelScroll = direction => btw.scroll(direction);
  const oauth = new McpAuthorizationOpener(text => { if (!stopping) view.message(text); }, openBrowser);
  let agentRender: ReturnType<typeof setTimeout> | undefined;
  const renderAgents = () => {
    if (agentRender || stopping) return;
    agentRender = setTimeout(() => { agentRender = undefined; if (!stopping) view.tui.requestRender(); }, 80);
  };
  let observedSession: Session | undefined;
  let unsubscribe: (() => void) | undefined;
  const observeBackground = () => {
    const session = runtime.session;
    if (!session || session === observedSession) return;
    unsubscribe?.();
    if (observedSession && !observedSession.isClosed) observedSession.setStaleReloadHandler(undefined);
    btw.reset(); view.updates.clear(); view.agents.clear(); oauth.clear(); observedSession = session;
    view.setSessionTitle(session.summary?.title ?? '');
    const warnings = async () => {
      try {
        const warnings = await session.getSessionWarnings();
        if (stopping || runtime.session !== session) return;
        for (const warning of warnings) view.notice(`Warning: ${warning.message}`, '', warning.severity === 'error' ? 'error' : 'warning');
      } catch { /* Startup warnings are best effort. */ }
    };
    session.setStaleReloadHandler(async () => {
      if (stopping || runtime.session !== session) return;
      btw.close(); view.agents.clear(); view.clearTranscript();
      view.message('Session changed on disk; reloaded the latest history.');
      await replay(runtime, view, true);
      if (pendingPrompt) view.userMessage(pendingPrompt);
      view.setSessionTitle(session.summary?.title ?? '');
      await Promise.all([refreshCommands(), refreshStatus(), warnings()]);
    });
    if (session.getResumeState()?.warning) view.notice(`Warning: ${session.getResumeState()!.warning}`);
    void warnings();
    unsubscribe = session.onEvent(event => {
      if (stopping || runtime.session !== session) return;
      if (event.type === 'tool.progress') oauth.handle(event);
      if (view.updates.handle(event) && event.type === 'tool.result') { view.hideTool(event.agentId, event.toolCallId); view.tui.requestRender(); }
      if (btw.owns(event.agentId)) return;
      const record = view.agents.apply(event);
      if (record) { view.transcript.agentActivity(record); renderAgents(); }
      if (event.agentId !== 'main') return;
      renderEventNotice(view, event);
      if (event.type === 'turn.step.interrupted') view.finishTurn(event.agentId);
      if (event.type === 'session.meta.updated') {
        const title = event.title ?? event.patch?.['title'];
        if (typeof title === 'string') view.setSessionTitle(title);
        void refreshStatus();
      }
      if (event.type === 'tool.list.updated') void refreshCommands().catch(error => view.error(error));
      if (event.type === 'turn.started') pendingPrompt = undefined;
      if (event.type === 'turn.ended') {
        view.agents.finishForeground();
        if (event.reason === 'completed') { void titles.completed(session.id); view.notifyCompletion(); }
      }
      if (observingTurn) return;
      activity.handle(event);
      switch (event.type) {
        case 'turn.started': view.beginAssistant(); break;
        case 'assistant.delta': view.delta(event.delta); break;
        case 'thinking.delta': view.thinkingDelta(event.delta); break;
        case 'tool.call.delta': view.toolDelta(event); break;
        case 'tool.call.started': view.toolStarted(event); break;
        case 'tool.progress': view.toolProgress(event.agentId, event.toolCallId, event.update.text ?? '', event.update.replace); break;
        case 'tool.result': view.toolResult(event); break;
        case 'turn.ended': view.finishTurn(event.agentId); if (!busy) view.setStatus('Idle'); break;
        case 'error': view.error(event); break;
        case 'skill.activated': view.message(`Skill activated: ${event.skillName}`); break;
      }
      if (event.type === 'shell.output' && event.commandId === shellId && event.update.text) view.shellDelta(event.update.text);
      else if (event.type === 'background.task.started' || event.type === 'background.task.terminated') view.message(`Background task: ${describeTask(event.info)}`);
      else if (event.type === 'mcp.server.status') view.message(`MCP: ${describeMcp(event.server)}`);
      else if (event.type === 'goal.updated') view.message(`Goal: ${describeGoal(event.snapshot)}`);
    });
  };
  const skillCommands = new Map<string, string>();
  const pluginCommands = new Map<string, { pluginId: string; name: string }>();
  const refreshCommands = async () => {
    const session = runtime.session;
    if (!session) return;
    const [skills, plugins] = await Promise.all([session.listSkills(), session.listPluginCommands()]);
    skillCommands.clear(); pluginCommands.clear();
    const dynamic: { name: string; description?: string; skillName?: string }[] = [];
    for (const skill of skills) {
      if (skill.scopes && !skill.scopes.includes('tui')) continue;
      if (skill.type && !['prompt', 'inline', 'flow'].includes(skill.type)) continue;
      const name = skill.source === 'builtin' || skill.isSubSkill ? skill.name : `skill:${skill.name}`;
      if (commands.some(command => command.name === name)) continue;
      skillCommands.set(name, skill.name); dynamic.push({ name, description: skill.description, skillName: skill.name });
    }
    for (const plugin of plugins) {
      const name = `${plugin.pluginId}:${plugin.name}`;
      if (commands.some(command => command.name === name) || skillCommands.has(name)) continue;
      pluginCommands.set(name, { pluginId: plugin.pluginId, name: plugin.name });
      dynamic.push({ name, description: plugin.description });
    }
    view.setCommands([...commands, ...dynamic], session.workDir, session.summary?.additionalDirs ?? []);
  };
  const queue: { text: string; attachments?: string[] }[] = [];
  const attachments: string[] = [...runtime.options.attachments ?? []];
  let clipboardDir: string | undefined;
  let pasteTask: Promise<boolean> | undefined;
  view.onPasteMedia = () => {
    pasteTask = (async () => {
      if (!runtime.session || stopping) return false;
      const media = await readClipboardMedia();
      if (!media || stopping) return false;
      if (media.kind === 'file') {
        const kind = mediaKindForPath(media.path);
        if (kind !== 'image' && kind !== 'video') return false;
        const source = await stat(media.path);
        if (!source.isFile() || !source.size) throw new Error('Clipboard media must be a nonempty regular file.');
        if (kind === 'video' && source.size > 100 * 1024 * 1024) throw new Error('Clipboard video exceeds the 100 MiB limit.');
        clipboardDir ??= await mkdtemp(join(tmpdir(), 'coderai-clipboard-'));
        const path = join(clipboardDir, `${randomUUID()}-${basename(media.path)}`);
        await copyFile(media.path, path);
        if (stopping) return true;
        const saved = await stat(path);
        if (saved.size !== source.size || (kind === 'video' && saved.size > 100 * 1024 * 1024)) { await rm(path, { force: true }); throw new Error('Clipboard media changed while being copied.'); }
        attachments.push(path);
      } else {
        clipboardDir ??= await mkdtemp(join(tmpdir(), 'coderai-clipboard-'));
        const path = join(clipboardDir, randomUUID());
        await writeFile(path, media.bytes, { mode: 0o600 });
        if (stopping) return true;
        attachments.push(path);
      }
      view.message(`Clipboard media staged for the next prompt (${attachments.length} total).`);
      await refreshStatus();
      return true;
    })();
    return pasteTask;
  };
  let statusVersion = 0;
  const refreshStatus = async () => {
    view.setQueue(queue);
    const session = runtime.session;
    const version = ++statusVersion;
    if (stopping) return;
    if (!session) { if (!busy && !view.dialogOpen) view.setStatus('Use /setup to connect a model · /help for commands'); return; }
    view.setWorkspaceStatus(session.workDir, attachments);
    try {
      const [status, config, todos, { goal }, tasks] = await Promise.all([session.getStatus(), runtime.harness.getConfig(), session.getTodos(), session.getGoal(), session.listBackgroundTasks({ activeOnly: true })]);
      if (stopping || runtime.session !== session || version !== statusVersion) return;
      const model = status.model ? config.models?.[status.model] : undefined;
      view.setSessionStatus(status, basename(session.workDir), model?.displayName || model?.model || status.model || 'No model', attachments.length, session.id);
      view.footer.setActivity(goal?.status ?? '', tasks.length);
      view.setTodos(todos);
      if (!busy && !view.dialogOpen) view.setStatus('Idle');
    } catch { /* The session may be closing. */ }
  };
  let finish!: () => void;
  const done = new Promise<void>(resolve => { finish = resolve; });
  const restoreActivity = () => {
    if (!busy || stopping || view.dialogOpen) return;
    if (shellId) view.setActivity('Running shell command');

    else activity.waiting();
  };
  const exit = () => {
    if (stopping) return; stopping = true;
    view.dismissDialog();
    btw.close();
    void runtime.session?.cancel().catch(() => {}); finish();
  };
  let lastExitRequest = 0;
  view.onExit = () => {
    if (busy && runtime.session && !view.dialogOpen && Date.now() - lastExitRequest > 3000) {
      lastExitRequest = Date.now();
      view.setStatus('Work is running · press Ctrl+D again within 3 seconds to cancel and exit');
      return;
    }
    exit();
  };
  view.onPlanToggle = () => {
    if (!busy && runtime.session) void submit('/plan');
  };
  view.onExternalEditor = () => {
    if (busy || editing) return;
    editing = true;
    void view.editExternally().catch(error => view.message(`External editor failed: ${String(error)}`)).finally(() => { editing = false; });
  };
  view.onInterrupt = () => { if (btw.cancel()) { view.setStatus('Cancelling side question…'); } else if (busy && runtime.session && !runtime.session.isClosed) { activity.cancel(); const cancellation = shellId ? runtime.session.cancelShellCommand(shellId) : runtime.session.cancel(); void cancellation.catch(error => view.message(`Cancellation failed: ${String(error)}`)); view.setStatus('Cancelling…'); } else if (btw.close()) { /* Return to main conversation. */ } else if (view.editor.getText()) { view.editor.setText(''); view.message('Draft cleared. Press Ctrl+C again to exit.'); } else exit(); };
  let lastIdleEscape = 0;
  view.onEscape = () => {
    if (btw.close()) return true;
    if (busy) { lastIdleEscape = 0; view.onInterrupt?.(); return true; }
    if (view.editor.getText()) { lastIdleEscape = 0; return false; }
    if (Date.now() - lastIdleEscape < 1000) {
      lastIdleEscape = 0;
      void submit('/undo').catch(error => view.message(String(error)));
    } else lastIdleEscape = Date.now();
    return true;
  };
  view.onSteer = () => {
    const text = view.editor.getText();
    if (!busy || !runtime.session || !text.trim()) return;
    if (shellId) { view.message('Steering is available during an agent turn.'); return; }
    if (attachments.length) { view.message('Steering does not support staged attachments. Send or clear them first.'); return; }
    const session = runtime.session;
    void session.steer(text).then(() => {
      if (view.editor.getText() === text) view.editor.setText('');
      view.message('Steering sent');
    }).catch(error => view.message(`Steering failed: ${String(error)}`));
  };
  view.onRecallQueue = () => {
    if (!busy || !queue.length) return false;
    const item = queue.pop()!;
    if (item.attachments?.length) attachments.unshift(...item.attachments);
    view.editor.setText(item.text);
    view.message('Queued input recalled.');
    void refreshStatus();
    return true;
  };
  view.onTaskBrowser = () => { if (runtime.session) void submit('/tasks').catch(error => view.error(error)); };
  view.onManageAttachments = () => {
    if (!attachments.length) { view.setStatus('No attachments staged.'); return; }
    const staged = [...attachments];
    void view.choose('Remove staged attachment\nChoose a file to remove. Esc keeps all files.', staged.map((path, index) => {
      const item = view.attachmentPane.entries[index];
      return { value: String(index), label: `${index + 1}. ${basename(path)}`, description: `${item?.mimeType ?? 'unknown'} · ${path}${item?.notice ? ` · ${item.notice}` : ''}` };
    })).then(async answer => {
      if (answer !== '' && attachments[Number(answer)] === staged[Number(answer)]) {
        attachments.splice(Number(answer), 1);
      }
      await refreshStatus();
      restoreActivity();
    }).catch(error => view.error(error));
  };
  const submit = async (text: string, queuedObjective?: string, queuedAttachments?: string[]) => {
    const match = text.match(/^(\/\S+)(?=\s|$)/);
    if (match && commandAliases[match[1]!]) text = commandAliases[match[1]!]! + text.slice(match[1]!.length);
    if (text === '/quit' || text === '/exit') { exit(); return; }
    if (!runtime.session && !busy) {
      if (/^\/(help|settings|config|reload-tui|theme|editor|experiments|experimental|doctor|secondary-model|subagent-model|plugins|skills)(?:\s|$)/.test(text)) { await command(runtime, view, text); await refreshStatus(); return; }
      if (text !== '/setup') { view.notice('Connect a model with /setup before starting a conversation.'); return; }
      busy = true;
      try {
        await command(runtime, view, text);
        if (stopping) return;
        await runtime.open(); observeBackground(); await refreshCommands();
        view.message(`Session ${runtime.session!.id}`);
        if (runtime.options.resume) await replay(runtime, view, true);
      } catch (error) { view.error(error); }
      finally { busy = false; if (!stopping) await refreshStatus(); }
      return;
    }
    if (runtime.session && /^\/(tasks|output|stop)(?:\s|$)/.test(text)) {
      try { await command(runtime, view, text); } catch (error) { view.error(error); }
      await refreshStatus(); restoreActivity(); return;
    }
    if (text === '/attach' || text.startsWith('/attach ')) {
      const arg = text.slice('/attach'.length).trim();
      if (!arg || arg === 'list') view.message(attachments.length ? `Attachments for the next prompt:\n${attachments.map((path, index) => `${index + 1}. ${path}`).join('\n')}` : 'No attachments staged.');
      else if (arg === 'clear') { attachments.length = 0; view.message('Attachments cleared.'); }
      else if (arg.startsWith('remove ')) {
        const index = Number(arg.slice(7));
        if (!Number.isInteger(index) || index < 1 || index > attachments.length) view.message('Usage: /attach remove NUMBER');
        else { attachments.splice(index - 1, 1); view.message(`Removed attachment ${index}.`); }
      } else {
        const path = arg.startsWith('-- ') ? arg.slice(3).trim() : arg;
        if (!path || path === '--') view.message('Usage: /attach PATH');
        else { attachments.push(path); view.message(`Attachment staged: ${path}`); }
      }
      await refreshStatus(); return;
    }
    if (text === '/goal next' || text.startsWith('/goal next ')) {
      try {
        const session = runtime.session;
        if (!session) { view.message('No active conversation.'); return; }
        const arg = text.slice('/goal next'.length).trim();
        if (!arg || arg === 'list') {
          const { goals } = await readGoalQueue(session);
          view.message(goals.length ? `Upcoming goals:\n${goals.map((goal, index) => `${index + 1}. ${goal.objective}`).join('\n')}` : 'No upcoming goals.');
          return;
        }
        if (arg === 'manage') {
          const { goals } = await readGoalQueue(session);
          if (!goals.length) { view.message('No upcoming goals.'); return; }
          const id = await view.choose('Choose an upcoming goal:', goals.map((goal, index) => ({ value: goal.id, label: `${index + 1}. ${goal.objective}` })));
          if (!id) return;
          const action = await view.choose('Choose an action:', [{ value: 'edit', label: 'Edit' }, { value: 'up', label: 'Move up' }, { value: 'down', label: 'Move down' }, { value: 'remove', label: 'Remove' }]);
          if (!action) return;
          if (action === 'remove') await removeGoalQueueItem(session, { goalId: id });
          else if (action === 'up' || action === 'down') await moveGoalQueueItem(session, { goalId: id, direction: action });
          else {
            const objective = await view.ask('Enter the replacement goal objective:', 'Enter objective · Esc cancel');
            if (!objective.trim()) return;
            await updateGoalQueueItem(session, { goalId: id, objective });
          }
          view.message('Upcoming goals updated.'); return;
        }
        const edit = arg.match(/^(remove|up|down)\s+(\d+)$/);
        if (edit) {
          const { goals } = await readGoalQueue(session); const goal = goals[Number(edit[2]) - 1];
          if (!goal) throw new Error('No upcoming goal with that number.');
          if (edit[1] === 'remove') await removeGoalQueueItem(session, { goalId: goal.id });
          else await moveGoalQueueItem(session, { goalId: goal.id, direction: edit[1] as 'up' | 'down' });
          view.message('Upcoming goals updated.'); return;
        }
        const objective = arg.startsWith('-- ') ? arg.slice(3).trim() : arg;
        const { goal } = await session.getGoal();
        if (!goal && !busy) { void submit(`/goal ${objective}`); return; }
        await appendGoalQueueItem(session, { objective }); view.message(`Upcoming goal queued: ${objective}`);
      } catch (error) { view.message(`Goal error: ${error instanceof Error ? error.message : String(error)}`); }
      return;
    }
    if (text === '/goal' || text === '/goal status' || text === '/goal pause' || text === '/goal cancel') {
      try {
        if (!runtime.session) { view.message('No active conversation.'); return; }
        if (text === '/goal pause' || text === '/goal cancel') {
          if (text.endsWith('pause')) await runtime.session.pauseGoal(); else await runtime.session.cancelGoal();
          if (runningGoal) await runtime.session.cancel();
          view.message(text.endsWith('pause') ? 'Goal paused.' : 'Goal cancelled.');
        } else {
          const { goal } = await runtime.session.getGoal();
          view.message(goal ? `Goal: ${goal.objective}\nStatus: ${goal.status} · turns ${goal.turnsUsed} · tokens ${goal.tokensUsed}` : 'No active goal.');
        }
      } catch (error) { view.message(`Goal error: ${error instanceof Error ? error.message : String(error)}`); }
      return;
    }
    if (text === '/queue' || text.startsWith('/queue ')) {
      const arg = text.slice('/queue'.length).trim();
      if (arg === 'clear') { queue.length = 0; view.message('Queue cleared.'); }
      else if (arg.startsWith('remove ')) {
        const index = Number(arg.slice(7));
        if (!Number.isInteger(index) || index < 1 || index > queue.length) view.message('Usage: /queue remove <number>');
        else { queue.splice(index - 1, 1); view.message(`Removed queued item ${index}.`); }
      } else if (arg) view.message('Usage: /queue [clear|remove NUMBER]');
      else view.message(queue.length ? `Queued input:\n${queue.map((item, index) => `${index + 1}. ${item.text}${item.attachments?.length ? ` (${item.attachments.length} attachments)` : ''}`).join('\n')}` : 'Queue is empty.');
      await refreshStatus(); return;
    }
    if (runtime.session && (text === '/auto-title' || text.startsWith('/auto-title '))) {
      const mode = text.slice('/auto-title'.length).trim();
      if (mode && mode !== 'on' && mode !== 'off') { view.message('Usage: /auto-title [on|off]'); return; }
      if (mode) {
        await saveAutoSessionTitle(runtime.harness.homeDir, mode === 'on'); await view.reloadPreferences();
      }
      view.message(`Automatic session titles: ${autoTitleEnabled ? 'on' : 'off'}`); return;
    }
    if (runtime.session && (text === '/btw' || text.startsWith('/btw '))) {
      const argument = text.slice(4).trim();
      if (argument === 'close') { if (btw.close()) view.message('Side question closed.'); return; }
      if (btw.isRunning) { view.message('Wait for /btw to finish before sending another question.'); return; }
      const question = argument || (await view.ask('Side question:', 'Enter question · Esc cancel')).trim();
      if (question && !stopping) await btw.open(runtime.session, question, inlineSkillActivations(question, skillCommands));
      return;
    }
    if (btw.isActive && !text.startsWith('/') && !text.startsWith('!')) {
      if (!btw.send(text, inlineSkillActivations(text, skillCommands))) { view.editor.setText(text); view.message('Wait for /btw to finish before sending another question.'); }
      return;
    }
    if (busy) {
      if (text.startsWith('/steer ') && shellId) view.message('Cannot steer a shell command; wait for it to finish or cancel it.');
      else if (text.startsWith('/steer ') && runtime.session) { await runtime.session.steer(text.slice(7)); view.message('Steering sent'); }
      else if (runtime.session && /^\/yolo(?:\s|$)/.test(text)) { await command(runtime, view, text); await refreshStatus(); }
      else if (runtime.session && ['/help', '/status', '/context', '/usage', '/mcp', '/skills', '/todos', '/version'].includes(text)) await command(runtime, view, text);
      else {
        const name = text.split(/\s+/)[0]!.slice(1);
        const takesMedia = swarmTask(text) !== undefined || !text.startsWith('/') && !text.startsWith('!') || text.startsWith('/goal ') || skillCommands.has(name) || pluginCommands.has(name);
        const staged = takesMedia ? attachments.splice(0) : undefined;
        queue.push({ text, attachments: staged }); view.message(`Queued (${queue.length}): ${text}`); await refreshStatus();
      }
      return;
    }
    busy = true;
    let followGoal = false;
    let turnReason: string | undefined;
    let reportedError = false;
    try {
      let dispatch: ((session: Session) => Promise<void>) | undefined;
      let prompt = text;
      let skill: string | undefined;
      let plugin: { pluginId: string; name: string } | undefined;
      let commandArgs = '';
      let goalAction: (() => Promise<unknown>) | undefined;
      if (text.startsWith('/goal ')) {
        const argument = text.slice(6).trim();
        if (!argument) { view.message('Usage: /goal <objective>|status|pause|resume|cancel|replace <objective>'); return; }
        if (argument === 'resume') { if (!queuedObjective) goalAction = () => runtime.session!.resumeGoal(); prompt = queuedObjective ?? 'Resume the active goal.'; }
        else {
          const replace = argument.startsWith('replace ');
          prompt = replace ? argument.slice(8).trim() : argument;
          if (!prompt) { view.message('Provide a goal objective.'); return; }
          goalAction = () => runtime.session!.createGoal({ objective: prompt, replace });
        }
        followGoal = true;
      } else if (text === '/tower teardown') {
        prompt = TOWER_TEARDOWN_PROMPT;
      } else if (swarmTask(text) !== undefined) {
        try {
          if (!await startSwarm(runtime, view, text, 'task')) {
            if (queuedAttachments?.length) attachments.unshift(...queuedAttachments);
            return;
          }
        } catch (error) {
          if (queuedAttachments?.length) attachments.unshift(...queuedAttachments);
          view.editor.setText(text); throw error;
        }
        prompt = swarmTask(text)!;
      } else if (text.startsWith('/')) {
        const previousSession = runtime.session?.id;
        if (await command(runtime, view, text)) {
          if (runtime.session?.id !== previousSession) attachments.length = 0;
          observeBackground(); await refreshCommands(); return;
        }
        const [rawName, ...args] = text.trim().split(/\s+/);
        const name = rawName!.slice(1);
        skill = skillCommands.get(name);
        plugin = pluginCommands.get(name);
        commandArgs = args.join(' ');
        if (!skill && !plugin) { view.message('Unknown command. Use /help.'); return; }
      }
      if (text.startsWith('!')) {
        const shellCommand = text.slice(1).trim();
        if (!shellCommand) { view.message('Enter a shell command after !'); return; }
        shellId = randomUUID();
        view.message(`Shell: ${shellCommand}`); view.setActivity('Running shell command', `$ ${shellCommand}`);
        view.beginShell();
        try {
          const result = await runtime.session!.runShellCommand(shellCommand, { commandId: shellId });
          if (result.stdout) view.message(result.stdout);
          if (result.stderr) view.message(result.stderr);
          view.message(result.backgrounded ? 'Shell command backgrounded.' : result.isError ? 'Shell command failed.' : 'Shell command completed.');
          if (result.isError) view.notice('Check the command output above, then edit the command and try again.');
        } finally { view.endShell(); shellId = undefined; }
      } else {
        const staged = queuedAttachments ?? [...attachments];
        let input: PromptInput | undefined;
        if (staged.length) {
          try { input = await attachmentPrompt(runtime.harness, runtime.session!, plugin ? commandArgs : prompt, staged, runtime.session!.workDir, plugin ? 'references' : 'parts'); }
          catch (error) {
            if (queuedAttachments) {
              attachments.unshift(...queuedAttachments);
              if (!view.editor.getText()) view.editor.setText(text);
              else view.message(`Failed queued prompt (copy to retry): ${text}`);
              view.message('Queued attachments restored. Correct them and submit the prompt again.');
            }
            throw error;
          }
        }
        if (goalAction) {
          try { await goalAction(); }
          catch (error) {
            if (queuedAttachments?.length) attachments.unshift(...queuedAttachments);
            throw error;
          }
        }
        if (followGoal) view.message(`Goal started: ${prompt}`);
        const inline = inlineSkillActivations(text, skillCommands);
        if (skill) {
          const activation = { name: skill, args: commandArgs || undefined };
          dispatch = session => session.promptWithSkills(input ?? prompt, [activation, ...inline.filter(item => item.name !== skill)]);
        } else if (plugin) {
          const { pluginId, name } = plugin;
          const args = input ? input.filter(part => part.type === 'text').map(part => part.text).join('\n\n') : commandArgs;
          dispatch = session => session.activatePluginCommand(pluginId, name, args || undefined);
        } else if (inline.length) {
          dispatch = session => session.promptWithSkills(input ?? prompt, inline);
        } else if (input) {
          const mediaInput = input;
          dispatch = session => session.prompt(mediaInput);
        }
        if (staged.length) {
          if (!queuedAttachments) attachments.splice(0, staged.length);
          view.setWorkspaceStatus(runtime.session!.workDir, attachments);
          view.message(`Attached: ${staged.join(', ')}`);
        }
        pendingPrompt = text; view.userMessage(text); activity.waiting();
        observingTurn = true;
        runningGoal = followGoal;
        let goalTurns = 0;
        const todoCalls = new Set<string>();
        try { turnReason = await runtime.run(prompt, event => {
          if (event.agentId !== 'main' || event.type.startsWith('subagent.')) return;
          activity.handle(event);
          switch (event.type) {
            case 'turn.started': if (event.agentId === 'main' && goalTurns++ > 0 && followGoal) { view.beginAssistant(); view.message('Continuing goal…'); } break;
            case 'assistant.delta': view.delta(event.delta); break;
            case 'tool.call.started':
              if (event.agentId === 'main' && event.name === 'TodoList') { todoCalls.add(event.toolCallId); view.hideTool(event.agentId, event.toolCallId); view.beginAssistant(false); }
              else view.toolStarted(event);
              break;
            case 'tool.call.delta':
              if (event.name === 'TodoList') todoCalls.add(event.toolCallId);
              if (!todoCalls.has(event.toolCallId)) view.toolDelta(event);
              break;
            case 'tool.progress': view.toolProgress(event.agentId, event.toolCallId, event.update.text ?? '', event.update.replace); break;
            case 'tool.result': {
              const todoCall = event.agentId === 'main' && todoCalls.delete(event.toolCallId);
              const todos = todoCall && !event.isError ? todoListFromResult(event.output) : undefined;
              if (todos !== undefined) { view.setTodos(todos); view.showTodos(todos); }
              else {
                if (todoCall) view.toolStarted({ agentId: event.agentId, toolCallId: event.toolCallId, name: 'TodoList' });
                view.toolResult(event);
              }
              if (todoCall) void refreshStatus();
              if (event.isError) view.notice('The action failed. The assistant can adjust it; use /steer to give another direction.');
              break;
            }
            case 'thinking.delta': view.thinkingDelta(event.delta); break;
            case 'error':
              if (event.agentId === 'main') { view.error(event); reportedError = true; }
              else view.notice(`Agent ${event.agentId}: ${event.message}`, '', 'error');
              break;
            case 'turn.ended':
              view.finishTurn(event.agentId);
              if (event.agentId === 'main' && event.reason === 'failed' && event.error && !reportedError) { view.error(event.error); reportedError = true; }
              break;

            case 'background.task.started': case 'background.task.terminated':
              view.message(`Background task: ${describeTask(event.info)}`); break;
            case 'mcp.server.status': view.message(`MCP: ${describeMcp(event.server)}`); break;
            case 'skill.activated': view.message(`Skill activated: ${event.skillName}`); break;
            case 'goal.updated':
              if (event.snapshot?.status === 'active') { followGoal = true; runningGoal = true; }
              view.message(`Goal: ${describeGoal(event.snapshot)}`); break;
            case 'agent.status.updated': if (event.agentId === 'main') void refreshStatus(); break;
            case 'compaction.completed': view.message('Context compaction completed.'); break;
            case 'compaction.cancelled': view.message('Context compaction cancelled.'); break;
            case 'compaction.blocked': view.notice('Context compaction is blocked.', 'Use /context to inspect usage, /model to change models, or /new to start fresh.'); break;
            case 'turn.step.completed':
              if (event.agentId === 'main' && event.providerFinishReason === 'filtered') view.notice('The provider filtered the response.', 'Revise the prompt or choose another model with /model.');
              else if (event.agentId === 'main' && event.finishReason === 'max_tokens') view.notice('The model reached its output limit.', 'The response or tool call may be incomplete. Ask the assistant to continue or check the model output limit.');
              break;
          }
        }, async request => {
          try {
            if (request.display.kind === 'plan_review') {
              const options = request.display.options && request.display.options.length >= 2 ? request.display.options : [{ label: 'Approve', description: 'Approve this plan' }];
              const choice = await view.choose(`Review plan\n${describeToolInput(request.display)}`, [
                ...options.map((option, i) => ({ value: `approve:${i}`, label: option.label, description: option.description })),
                { value: 'reject', label: 'Reject' }, { value: 'revise', label: 'Request revision' },
              ]);
              if (!choice) return { decision: 'cancelled' };
              if (choice === 'revise') {
                const feedback = await view.ask('What should change?', 'Enter feedback · Esc cancel');
                return feedback.trim() ? { decision: 'rejected', selectedLabel: 'Revise', feedback } : { decision: 'cancelled' };
              }
              if (choice === 'reject') return { decision: 'rejected', selectedLabel: 'Reject' };
              return { decision: 'approved', selectedLabel: options[Number(choice.slice(8))]?.label };
            }
            const answer = await view.approve(request);
            const normalized = answer.trim().toLowerCase();
            if (normalized === '/yolo' || normalized === '/yolo on') {
              await runtime.session!.setPermission('yolo');
              view.message('YOLO enabled · routine actions are approved automatically. Use /yolo off to return to manual approval.');
              return { decision: 'approved' };
            }
            if (normalized === 'always') return { decision: 'approved', scope: 'session' };
            if (normalized === 'feedback') {
              const feedback = (await view.ask('Why should this action change?', 'Enter rejection feedback · Esc cancel')).trim();
              return feedback ? { decision: 'rejected', feedback } : { decision: 'cancelled' };
            }
            if (normalized.startsWith('no:')) return { decision: 'rejected', feedback: answer.trim().slice(3).trim() || undefined };
            return { decision: normalized === 'yes' ? 'approved' : 'rejected' };
          } finally { activity.waiting(); }
        }, async request => {
          try {
            return await view.answerQuestions(request);
          } finally { activity.waiting(); }
        }, dispatch, followGoal, true); } catch (error) {
          if (followGoal) await runtime.session?.pauseGoal().catch(() => {});
          throw error;
        } finally { observingTurn = false; runningGoal = false; pendingPrompt = undefined; }
        if (turnReason !== 'completed') {
          view.message(`Turn ${turnReason}`);
          if (turnReason === 'failed' && !reportedError) view.notice('The turn could not finish.', 'Use ↑ to recall your prompt, or /export-debug to save diagnostics.');
          if (turnReason === 'blocked') view.notice('The turn is blocked.', 'Review the messages above and give the assistant the missing information or a new direction.');
        }
      }
    } catch (error) { if (!reportedError) view.error(error); }
    finally {
      view.finishTurn();
      busy = false;
      if (!stopping) {
        view.setStatus('Idle');
        await refreshStatus();
        if (turnReason === 'completed' && runtime.session) {
          try {
            const session = runtime.session; const { goal } = await session.getGoal();
            if (!goal || goal.status === 'complete') {
              const nextGoal = (await readGoalQueue(session)).goals[0];
              if (nextGoal) {
                await session.createGoal({ objective: nextGoal.objective });
                await removeGoalQueueItem(session, { goalId: nextGoal.id });
                view.message(`Starting upcoming goal: ${nextGoal.objective}`);
                void submit('/goal resume', nextGoal.objective); return;
              }
            }
          } catch (error) { view.message(`Upcoming goal error: ${error instanceof Error ? error.message : String(error)}`); }
        }
        const next = queue.shift();
        view.setQueue(queue);
        if (next !== undefined) void submit(next.text, undefined, next.attachments);
      }
    }
  };
  view.onSubmit = text => { void submit(text).catch(error => view.message(String(error))); };
  process.on('SIGTERM', exit); process.on('SIGINT', exit);
  view.start();
  try {
    try {
      await runtime.trust((text, workDir) => view.trustWorkspace(workDir, !runtime.session, text));
    } catch (error) {
      if (stopping) return;
      throw error;
    }
    if (stopping) return;
    try { await runtime.open(); }
    catch (error) {
      if (!(error instanceof ModelSetupRequired)) throw error;
      view.notice(error.message, 'Use /setup to connect a provider and model.');
    }
    if (runtime.session) {
      observeBackground(); await refreshCommands();
      if (stopping) return;
      view.message(`Session ${runtime.session.id}`);
      if (runtime.options.resume) await replay(runtime, view, true);
    }
    if (stopping) return;
    busy = false;
    await refreshStatus();
    const next = queue.shift();
    view.setQueue(queue);
    if (next !== undefined) void submit(next.text, undefined, next.attachments);
    await done;
  } finally { await btw.dispose(); if (agentRender) clearTimeout(agentRender); unsubscribe?.(); if (observedSession && !observedSession.isClosed) observedSession.setStaleReloadHandler(undefined); view.stop(); await view.flushHistory(); if (pasteTask) await pasteTask.catch(() => {}); if (clipboardDir) await rm(clipboardDir, { recursive: true, force: true }); process.off('SIGTERM', exit); process.off('SIGINT', exit); }
}
