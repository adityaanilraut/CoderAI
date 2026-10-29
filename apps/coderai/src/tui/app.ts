import type { Runtime } from '../runtime.js';
import { TerminalView } from './view.js';
import { command, replay, help } from './commands.js';
import { randomUUID } from 'node:crypto';
import { describeGoal, describeMcp, describeTask, describeToolInput } from './display.js';
import type { Session } from '@coderai/sdk';
import { basename } from 'node:path';
import { join } from 'node:path';
import { readFile } from 'node:fs/promises';
import { copyFile, mkdtemp, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { setTheme } from './theme.js';
import { appendGoalQueueItem, readGoalQueue, removeGoalQueueItem, moveGoalQueueItem, updateGoalQueueItem } from './goal-queue-store.js';
import { attachmentPrompt } from '../attachments.js';
import { readClipboardMedia } from '../clipboard-media.js';
import { mediaKindForPath } from '../../../../packages/agent-core-v2/src/agent/media/mediaRef.js';

const commandAliases: Record<string, string> = { '/h': '/help', '/?': '/help', '/yes': '/yolo', '/thinking': '/effort', '/task': '/tasks', '/export': '/export-md', '/q': '/quit', '/auto': '/permissions auto' };

export async function interactive(runtime: Runtime): Promise<void> {
  try {
    const theme = (await readFile(join(runtime.harness.homeDir, 'theme'), 'utf8')).trim();
    if (!await setTheme(theme, runtime.harness.homeDir)) await setTheme('dark', runtime.harness.homeDir);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const commands = [...Object.entries(commandAliases).map(([name, target]) => ({ name: name.slice(1), description: `Alias for ${target}` })), ...help.split('\n').map(line => {
    const match = line.match(/^\/(\S+)\s+(.*)$/);
    return { name: match?.[1] ?? '', description: match?.[2] ?? '' };
  }).filter(item => item.name)];
  const view = new TerminalView(runtime.harness.homeDir, runtime.options.cwd, commands);
  let busy = true;
  let stopping = false;
  let shellId: string | undefined;
  let editing = false;
  let observingTurn = false;
  let runningGoal = false;
  let sideSession: Session | undefined;
  let sideAgentId: string | undefined;
  let sideTask: Promise<void> | undefined;
  let observedSession: Session | undefined;
  let unsubscribe: (() => void) | undefined;
  const observeBackground = () => {
    const session = runtime.session;
    if (!session || session === observedSession) return;
    unsubscribe?.(); observedSession = session;
    unsubscribe = session.onEvent(event => {
      if (observingTurn) return;
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
    const dynamic: { name: string; description?: string }[] = [];
    for (const skill of skills) {
      if (skill.scopes && !skill.scopes.includes('tui')) continue;
      if (skill.type && !['prompt', 'inline', 'flow'].includes(skill.type)) continue;
      const name = skill.source === 'builtin' || skill.isSubSkill ? skill.name : `skill:${skill.name}`;
      if (commands.some(command => command.name === name)) continue;
      skillCommands.set(name, skill.name); dynamic.push({ name, description: skill.description });
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
  const attachments: string[] = [];
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
  const refreshStatus = async () => {
    view.setQueue(queue);
    if (!runtime.session || stopping || view.dialogOpen) return;
    try {
      const status = await runtime.session.getStatus();
      if (stopping || view.dialogOpen) return;
      view.setStatus(`${busy ? 'Working' : 'Idle'} · ${status.model ?? 'no model'} · ${status.thinkingEffort} · ${status.permission}${status.planMode ? ' · plan' : ''} · ${basename(runtime.session.workDir)} · context ${status.contextTokens}/${status.maxContextTokens} · queued ${queue.length}${attachments.length ? ` · attachments ${attachments.length}` : ''}`);
    } catch { /* The session may be closing. */ }
  };
  let finish!: () => void;
  const done = new Promise<void>(resolve => { finish = resolve; });
  const exit = () => {
    if (stopping) return; stopping = true;
    view.dismissDialog();
    if (sideSession && sideAgentId) void runtime.harness.withInteractiveAgent(sideAgentId, () => sideSession!.cancel()).catch(() => {});
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
  view.onInterrupt = () => { if (sideSession && sideAgentId) { void runtime.harness.withInteractiveAgent(sideAgentId, () => sideSession!.cancel()).catch(error => view.message(`Cancellation failed: ${String(error)}`)); view.setStatus('Cancelling side question…'); } else if (busy && runtime.session && !runtime.session.isClosed) { const cancellation = shellId ? runtime.session.cancelShellCommand(shellId) : runtime.session.cancel(); void cancellation.catch(error => view.message(`Cancellation failed: ${String(error)}`)); view.setStatus('Cancelling…'); } else if (view.editor.getText()) { view.editor.setText(''); view.message('Draft cleared. Press Ctrl+C again to exit.'); } else exit(); };
  let lastIdleEscape = 0;
  view.onEscape = () => {
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
    if (shellId || sideSession) { view.message('Steering is available during an agent turn.'); return; }
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
  const submit = async (text: string, queuedObjective?: string, queuedAttachments?: string[]) => {
    const match = text.match(/^(\/\S+)(?=\s|$)/);
    if (match && commandAliases[match[1]!]) text = commandAliases[match[1]!]! + text.slice(match[1]!.length);
    if (text === '/quit' || text === '/exit') { exit(); return; }
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
    if (busy) {
      if (text.startsWith('/steer ') && shellId) view.message('Cannot steer a shell command; wait for it to finish or cancel it.');
      else if (text.startsWith('/steer ') && runtime.session) { await runtime.session.steer(text.slice(7)); view.message('Steering sent'); }
      else if (runtime.session && /^\/yolo(?:\s|$)/.test(text)) { await command(runtime, view, text); await refreshStatus(); }
      else if (runtime.session && ['/help', '/status', '/context', '/usage', '/mcp', '/skills', '/todos', '/version'].includes(text)) await command(runtime, view, text);
      else { const staged = !text.startsWith('/') && !text.startsWith('!') ? attachments.splice(0) : undefined; queue.push({ text, attachments: staged }); view.message(`Queued (${queue.length}): ${text}`); await refreshStatus(); }
      return;
    }
    busy = true;
    let followGoal = false;
    let turnReason: string | undefined;
    try {
      let dispatch: ((session: Session) => Promise<void>) | undefined;
      let prompt = text;
      if (text === '/btw' || text.startsWith('/btw ')) {
        const question = text.slice(4).trim() || (await view.ask('Side question:', 'Enter question · Esc cancel')).trim();
        if (!question) return;
        sideTask = (async () => {
          const side = await runtime.harness.forkSession({ id: runtime.session!.id });
          sideSession = side;
          try {
          side.setApprovalHandler(() => ({ decision: 'rejected', feedback: 'Side questions do not approve changes.' }));
          side.setQuestionHandler(() => null);
          sideAgentId = await side.startBtw();
          if (stopping) return;
          const agentId = sideAgentId;
          view.message(`Side question: ${question}`); view.beginAssistant(); view.setStatus('Answering side question… · Ctrl+C cancel');
          await new Promise<void>((resolve, reject) => {
            let settled = false;
            const finish = (error?: Error) => { if (settled) return; settled = true; off(); error ? reject(error) : resolve(); };
            const off = side.onEvent(event => {
              if (event.agentId !== agentId) return;
              if (event.type === 'assistant.delta') view.delta(event.delta);
              else if (event.type === 'turn.ended') {
                if (event.reason !== 'completed') view.message(`Side question ${event.reason}.`);
                finish();
              }
            });
            void runtime.harness.withInteractiveAgent(agentId, () => side.prompt(question)).catch(error => finish(error instanceof Error ? error : new Error(String(error))));
          });
          } finally {
            sideAgentId = undefined; sideSession = undefined;
            await runtime.harness.deleteSession(side.id);
          }
        })();
        try { await sideTask; } finally { sideTask = undefined; }
        return;
      } else if (text.startsWith('/goal ')) {
        if ((queuedAttachments ?? attachments).length) { view.message('Attachments are not supported with goals. Clear them with /attach clear or send them in a regular prompt.'); return; }
        const argument = text.slice(6).trim();
        if (!argument) { view.message('Usage: /goal <objective>|status|pause|resume|cancel|replace <objective>'); return; }
        if (argument === 'resume') { if (!queuedObjective) await runtime.session!.resumeGoal(); prompt = queuedObjective ?? 'Resume the active goal.'; }
        else {
          const replace = argument.startsWith('replace ');
          prompt = replace ? argument.slice(8).trim() : argument;
          if (!prompt) { view.message('Provide a goal objective.'); return; }
          await runtime.session!.createGoal({ objective: prompt, replace });
        }
        followGoal = true;
        view.message(`Goal started: ${prompt}`);
      } else if (text.startsWith('/')) {
        if (await command(runtime, view, text)) { if (text === '/new' || text === '/resume' || text.startsWith('/resume ')) attachments.length = 0; observeBackground(); await refreshCommands(); return; }
        const [rawName, ...args] = text.trim().split(/\s+/);
        const name = rawName!.slice(1);
        const skill = skillCommands.get(name);
        const plugin = pluginCommands.get(name);
        if ((skill || plugin) && (queuedAttachments ?? attachments).length) { view.message('Attachments are not supported with skill or plugin commands. Clear them with /attach clear or send them in a regular prompt.'); return; }
        if (skill) dispatch = session => session.promptWithSkills(text, [{ name: skill, args: args.join(' ') || undefined }]);
        else if (plugin) dispatch = session => session.activatePluginCommand(plugin.pluginId, plugin.name, args.join(' ') || undefined);
        else { view.message('Unknown command. Use /help.'); return; }
      }
      if (text.startsWith('!')) {
        const shellCommand = text.slice(1).trim();
        if (!shellCommand) { view.message('Enter a shell command after !'); return; }
        shellId = randomUUID();
        view.message(`Shell: ${shellCommand}`); view.setStatus('Running shell command… · Ctrl+C cancel');
        view.beginShell();
        try {
          const result = await runtime.session!.runShellCommand(shellCommand, { commandId: shellId });
          if (result.stdout) view.message(result.stdout);
          if (result.stderr) view.message(result.stderr);
          view.message(result.backgrounded ? 'Shell command backgrounded.' : result.isError ? 'Shell command failed.' : 'Shell command completed.');
        } finally { view.endShell(); shellId = undefined; }
      } else {
        const staged = queuedAttachments ?? [...attachments];
        if (!dispatch && !followGoal && staged.length) {
          let input;
          try { input = await attachmentPrompt(runtime.harness, runtime.session!, prompt, staged, runtime.session!.workDir); }
          catch (error) {
            if (queuedAttachments) {
              attachments.unshift(...queuedAttachments);
              if (!view.editor.getText()) view.editor.setText(text);
              else view.message(`Failed queued prompt (copy to retry): ${text}`);
              view.message('Queued attachments restored. Correct them and submit the prompt again.');
            }
            throw error;
          }
          dispatch = session => session.prompt(input);
          if (!queuedAttachments) attachments.splice(0, staged.length);
          view.message(`Attached: ${staged.join(', ')}`);
        }
        view.message(`You: ${text}`); view.beginAssistant(); view.setStatus('Working… · Ctrl+C cancel');
        observingTurn = true;
        runningGoal = followGoal;
        let goalTurns = 0;
        try { turnReason = await runtime.run(prompt, event => {
          if (event.agentId !== 'main' && (event.type === 'assistant.delta' || event.type === 'thinking.delta')) return;
          switch (event.type) {
            case 'turn.started': if (event.agentId === 'main' && goalTurns++ > 0 && followGoal) { view.beginAssistant(); view.message('Continuing goal…'); } break;
            case 'assistant.delta': view.delta(event.delta); break;
            case 'tool.call.started': view.detail(`${event.agentId === 'main' ? 'Tool' : `Agent ${event.agentId} · tool`}: ${event.name}${event.description ? ` · ${event.description}` : ''}`, event.display ? describeToolInput(event.display) : JSON.stringify(event.args, null, 2) ?? ''); view.beginAssistant(false); break;
            case 'tool.result': view.detail(`${event.agentId === 'main' ? 'Tool' : `Agent ${event.agentId} · tool`} ${event.isError ? 'failed' : 'completed'}:`, typeof event.output === 'string' ? event.output : JSON.stringify(event.output, null, 2) ?? ''); break;
            case 'tool.progress': view.detail(`${event.agentId === 'main' ? 'Tool' : `Agent ${event.agentId} · tool`} progress:`, typeof event.update.text === 'string' ? event.update.text : JSON.stringify(event.update)); break;
            case 'thinking.delta': view.thinkingDelta(event.delta); view.setStatus('Thinking… · Ctrl+C cancel'); break;
            case 'error': view.message(`Error: ${JSON.stringify(event)}`); break;
            case 'warning': view.message(`Warning: ${JSON.stringify(event)}`); break;
            case 'subagent.spawned': view.message(`Agent ${event.subagentId} · ${event.subagentName} · started${event.description ? ` · ${event.description}` : ''}`); break;
            case 'subagent.started': view.message(`Agent ${event.subagentId} · running`); break;
            case 'subagent.suspended': view.message(`Agent ${event.subagentId} · suspended · ${event.reason}`); break;
            case 'subagent.completed': view.detail(`Agent ${event.subagentId} · completed`, event.resultSummary); break;
            case 'subagent.failed': view.message(`Agent ${event.subagentId} · failed · ${event.error}`); break;
            case 'subagent.cancelled': view.message(`Agent ${event.subagentId} · cancelled`); break;
            case 'background.task.started': case 'background.task.terminated':
              view.message(`Background task: ${describeTask(event.info)}`); break;
            case 'mcp.server.status': view.message(`MCP: ${describeMcp(event.server)}`); break;
            case 'skill.activated': view.message(`Skill activated: ${event.skillName}`); break;
            case 'goal.updated':
              if (event.snapshot?.status === 'active') { followGoal = true; runningGoal = true; }
              view.message(`Goal: ${describeGoal(event.snapshot)}`); break;
            case 'agent.status.updated': if (event.agentId === 'main') void refreshStatus(); break;
            case 'turn.step.retrying': view.setStatus('Retrying model request… · Ctrl+C cancel'); break;
            case 'compaction.started': view.setStatus('Compacting context…'); break;
            case 'compaction.completed': view.message('Context compaction completed.'); break;
            case 'compaction.cancelled': view.message('Context compaction cancelled.'); break;
          }
        }, async request => {
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
          try {
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
          } finally { view.setStatus('Working… · Ctrl+C cancel'); }
        }, async request => {
          const answers: Record<string, string> = {};
          for (const question of request.questions) {
            const heading = `${question.header ? question.header + '\n' : ''}${question.question}${question.body ? '\n' + question.body : ''}`;
            let answer: string;
            if (!question.multiSelect && question.options.length) {
              const choice = await view.choose(heading, [
                ...question.options.map((o, i) => ({ value: String(i), label: o.label, description: o.description })),
                { value: 'other', label: question.otherLabel ?? 'Other', description: question.otherDescription },
              ]);
              if (!choice) return null;
              answer = choice === 'other' ? await view.ask('Enter your answer:', 'Waiting for your answer · Enter submit · Esc dismiss') : question.options[Number(choice)]?.label ?? '';
            } else if (question.multiSelect && question.options.length) {
              const choices = await view.chooseMultiple(heading, question.options, question.otherLabel);
              if (!choices?.length) return null;
              answer = choices.join(', ');
            } else {
              answer = await view.ask(`${heading}\n${question.options.map((o, i) => `${i + 1}. ${o.label}${o.description ? ': ' + o.description : ''}`).join('\n')}\nEnter answers or comma-separated numbers.`, 'Waiting for your answer · Enter submit · Esc dismiss');
            }
            if (!answer.trim()) return null;
            answers[question.question] = answer;
          }
          view.setStatus('Working… · Ctrl+C cancel');
          return answers;
        }, dispatch, followGoal, true); } catch (error) {
          if (followGoal) await runtime.session?.pauseGoal().catch(() => {});
          throw error;
        } finally { observingTurn = false; runningGoal = false; }
        if (turnReason !== 'completed') view.message(`Turn ${turnReason}`);
      }
    } catch (error) { view.message(`Error: ${error instanceof Error ? error.message : String(error)}`); }
    finally {
      busy = false;
      if (!stopping) {
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
    await runtime.open();
    observeBackground();
    await refreshCommands();
    if (stopping) return;
    view.message(`Session ${runtime.session!.id} · ${runtime.session!.workDir}`);
    if (runtime.options.resume) await replay(runtime, view, true);
    if (stopping) return;
    busy = false;
    await refreshStatus();
    const next = queue.shift();
    view.setQueue(queue);
    if (next !== undefined) void submit(next.text, undefined, next.attachments);
    await done;
  } finally { if (sideTask) await sideTask.catch(() => {}); unsubscribe?.(); view.stop(); if (pasteTask) await pasteTask.catch(() => {}); if (clipboardDir) await rm(clipboardDir, { recursive: true, force: true }); process.off('SIGTERM', exit); process.off('SIGINT', exit); }
}
