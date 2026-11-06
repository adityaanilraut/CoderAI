import { showPlugins } from './plugins.js';
import { unifiedDiff } from './diff.js';
import { doctor } from '../config-commands.js';
import { showExperiments } from './experiments.js';
import { showSettings } from './settings.js';
import { saveTuiConfig } from './config.js';
import { startSwarm } from './modes.js';
import { parseTurn } from '../session-operations.js';
import { showImportSessions } from '../session-import/command.js';
import { undoablePrompts } from './undo.js';
import { identity } from '../identity.js';
import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { latestSessionId } from '../session-selection.js';
import { mkdir, rename, unlink, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { listThemes, setTheme, themeCheckpoint } from './theme.js';
import { exportMarkdown } from '../session-export.js';
import { chooseModel, configureModels } from './model-picker.js';
import { browseSessions } from './session-picker.js';
import { groupedHelp } from './help.js';
import { authenticateMcp } from './mcp-auth.js';
import { readMcpConfiguration, removeMcpConfiguration, testMcpConfiguration, resetMcpAuthentication } from '../mcp-operations.js';

export const help = `/help                         Show commands and keyboard shortcuts
/new                          New saved conversation
/sessions                     Browse and resume conversations
/import-sessions              Import recent Claude Code and Codex sessions
/import                       Alias for /import-sessions
/resume [id|latest]           Resume a conversation
/title [title]                Show or set the session title
/status                       Show session and runtime status
/btw [question|close]         Open or close an in-session side question
/auto-title [on|off]          Configure automatic session titles
/model [alias]                Choose a provider and model, or select an alias
/secondary-model [alias]      Pick the default subagent model
/subagent-model               Alias for /secondary-model
/experiments                  Inspect and toggle engine experiments
/experimental                 Alias for /experiments
/settings                     Configure TUI preferences
/config                       Alias for /settings
/reload-tui                   Reload ~/.coderai/tui.toml
/cron [list|delete ID]         Inspect or cancel scheduled jobs
/agents                       List resolved agent profiles
/hooks                        List configured hooks
/doctor                       Check local configuration and dependencies
/setup                        Add providers and models with guided setup
/provider [refresh]           Manage providers or refresh registry models
/permissions [manual|yolo|auto] Inspect or change approvals
/permission                   Choose approval mode
/yolo [on|off|status]         Toggle automatic approval
/context                      Show context usage
/usage                        Show token usage
/compact [instruction]        Compact context through the model
/clear                        Clear conversation context
/plan [on|off|show|clear]     Control plan mode or the current plan
/swarm [on|off|status|task]   Toggle Swarm or run a one-shot task
/tower [status|on|off|teardown|base] Control Tower mode
/goal <objective|status|pause|resume|cancel|next> Manage an autonomous goal
/tasks                        List background tasks
/stop <task-id>                Stop a background task
/output <task-id>              Open live, scrollable task output
/effort <level>                Set thinking effort
/mcp [action]                 Manage servers: add, remove, test, auth, logout, reconnect
/skills                       List available skills
/plugins [action]             Interactive plugins, info, MCP toggles and capabilities
/reload                       Reload the session configuration
/todos                        Show the current task list
/version                      Show CoderAI version
/add-dir [path|list]          List or add workspace directories
/init                         Generate project instructions
/fork [turn]                  Fork through a zero-based turn (default: all)
/diff [turn]                  Inspect recorded turn file changes (zero-based ID)
/undo [count]                 Withdraw prompts from context
/export-md <path>             Export the conversation as Markdown
/export-debug [path]          Export a session diagnostic archive
/theme [auto|dark|light|name] Choose terminal colors
/copy                         Copy the last assistant message (OSC 52)
/editor <command>             Set the external editor for Ctrl+G
/attach [path|list|clear|remove NUMBER] Stage media for the next prompt
/steer <text>                 Steer active work
/queue [clear|remove NUMBER]  Inspect or edit queued input
/quit                         Save and exit

Shortcuts: Enter send · Shift+Enter newline · Shift+Tab plan mode
Ctrl+G external editor · Ctrl+O expand tools and thinking · Ctrl+S steer
Ctrl+T remove attachments · Ctrl+B background tasks
↑ recalls prompt history · On empty input, ↑ recalls queued work first
Esc cancels work · Esc twice opens undo when idle
Ctrl+C cancel or quit · Ctrl+D quit (twice while work is running)
Fullscreen: Ctrl+Shift+F transcript search · / searches while scrolled up
PgUp/PgDn or mouse wheel scroll · Enter next match · Shift+Enter previous
Esc closes a dialog or search and returns to your draft`;

export async function command(runtime: Runtime, view: TerminalView, input: string): Promise<boolean> {
  const [name, ...args] = input.trim().split(/\s+/); const argument = args.join(' ');
  const session = runtime.session!;
  switch (name) {
    case '/help': await view.showDocument('CoderAI help', groupedHelp(help)); break;
    case '/setup': await configureModels(runtime, view); view.message('Setup closed. Configuration refreshed.'); break;
    case '/provider': {
      if (!argument) { await configureModels(runtime, view); view.message('Setup closed. Configuration refreshed.'); }
      else if (argument === 'refresh') {
        const result = await runtime.harness.refreshProviderModels();
        await session.reloadSession(); view.onSessionReloaded?.();
        view.message(result.changed.map(item => `${item.provider_id}: +${item.added} / -${item.removed} models`).join('\n') || 'No model changes.');
        if (result.failed.length) view.notice(`Refresh failed for: ${result.failed.map(item => item.provider).join(', ')}.`);
      } else throw new Error('Usage: /provider [refresh]');
      break;
    }
    case '/import': case '/import-sessions': await showImportSessions(runtime, view); break;
    case '/new': await runtime.open(''); view.clearTranscript(); view.message(`New session ${runtime.session!.id}`); break;
    case '/sessions': case '/resume':
      { let selected = argument;
        if (!selected) {
          selected = await browseSessions(runtime, view);
          if (!selected) break;
        }
        const id = selected === 'latest' ? await latestSessionId(runtime.harness, session.workDir) : selected;
        if (!id) throw new Error('No saved conversation in this workspace.');
        const freshResume = id !== session.id;
        await runtime.open(id); view.clearTranscript(); view.message(`Resumed ${runtime.session!.id}`); await replay(runtime, view, freshResume); break; }
    case '/rename': case '/title':
      if (argument.trim()) { await runtime.harness.renameSession({ id: session.id, title: argument.trim() }); view.message(`Title: ${argument.trim()}`); }
      else { const saved = (await runtime.harness.listSessions({ sessionId: session.id }))[0]; view.message(`Title: ${saved?.title ?? '(untitled)'}`); }
      break;
    case '/status': { const status = await session.getStatus(); view.message(`Session: ${session.id}\nWorkspace: ${session.workDir}\nModel: ${status.model ?? '(none)'}\nThinking effort: ${status.thinkingEffort}\nPermissions: ${status.permission}\nPlan mode: ${status.planMode ? 'on' : 'off'}\nSwarm: ${status.swarmMode ? 'on' : 'off'} · Tower: ${status.towerMode ? 'on' : 'off'}\nContext: ${status.contextTokens}/${status.maxContextTokens} tokens`); break; }
    case '/model':
      if (argument) { await runtime.validateModel(argument); await session.setModel(argument); view.message(`Model: ${argument}`); }
      else await chooseModel(runtime, view);
      break;
    case '/settings': case '/config': { const next = await showSettings(runtime, view); if (next) { if (!runtime.session && ['/model', '/permission'].includes(next)) view.notice('Use /setup to connect a model and start a session first.'); else await command(runtime, view, next); } break; }
    case '/reload-tui': await view.reloadPreferences(); view.message('TUI preferences reloaded.'); break;
    case '/experiments': case '/experimental': await showExperiments(runtime, view); break;
    case '/secondary-model': case '/subagent-model': {
      const config = await runtime.harness.getConfig({ reload: true });
      const models = Object.entries(config.models ?? {}).filter(([alias]) => alias !== 'primary' && !alias.startsWith('__'));
      const current = config.secondaryModel?.defaultModel ?? config.secondaryModel?.model;
      const selected = argument || await view.chooseSearchable('Default subagent model', models.map(([alias, model]) => ({ value: alias, label: `${alias}${alias === current ? ' (current)' : ''}`, description: `${model.provider} · ${model.model}` })));
      if (!selected) { if (!models.length) view.message('No saved models available. Use /setup.'); break; }
      if (selected === 'primary') throw new Error('The alias primary is reserved for the caller’s own model.');
      if (!models.some(([alias]) => alias === selected)) throw new Error(`Unknown saved model alias: ${selected}`);
      await runtime.validateModel(selected);
      const pool = config.secondaryModel?.models;
      await runtime.harness.setConfig({ secondaryModel: { defaultModel: selected, ...(pool ? { models: { ...pool, [selected]: pool[selected] ?? '' } } : {}) } });
      view.message(`Secondary model: ${selected}. Newly spawned subagents will use it.`); break;
    }
    case '/cron': {
      if (args[0] === 'delete' && args.length === 2) { view.message(await session.deleteCronTask(args[1]!) ? `Scheduled job deleted: ${args[1]}` : `Scheduled job not found: ${args[1]}`); break; }
      if (argument && argument !== 'list') throw new Error('Usage: /cron [list|delete ID]');
      const { tasks } = await session.getCronTasks();
      view.message(tasks.length ? tasks.map(task => `${task.id} · ${task.cron} · ${task.recurring ? 'recurring' : 'one-shot'} · next ${task.nextFireAt ? new Date(task.nextFireAt).toLocaleString() : '(none)'}`).join('\n') : 'No scheduled jobs.'); break;
    }
    case '/agents': { const profiles = await runtime.harness.listAgentProfiles(session.id); await view.showDocument('Agent profiles', profiles.map(profile => `${profile.name}${profile.description ? ` — ${profile.description}` : ''}${profile.whenToUse ? `\n${profile.whenToUse}` : ''}`).join('\n\n') || 'No agent profiles available.'); break; }
    case '/hooks': {
      const { hooks } = await runtime.harness.getConfig();
      await view.showDocument('Configured hooks', hooks && Object.keys(hooks).length ? JSON.stringify(hooks, null, 2) : 'No hooks configured.'); break;
    }
    case '/doctor': { const lines: string[] = []; const code = await doctor(runtime, text => lines.push(text.trimEnd()), text => lines.push(text.trimEnd())); await view.showDocument(`Local diagnostics${code ? ' — issues found' : ' — OK'}`, lines.join('\n')); break; }
    case '/permissions':
      if (!argument) view.message(`Permissions: ${(await session.getStatus()).permission}`);
      else if (argument === 'manual' || argument === 'yolo' || argument === 'auto') { await session.setPermission(argument); view.message(`Permissions: ${argument}`); }
      else throw new Error('Usage: /permissions manual|yolo|auto');
      break;
    case '/yolo': {
      if (argument && !['on', 'off', 'status'].includes(argument)) throw new Error('Usage: /yolo [on|off|status]');
      const current = (await session.getStatus()).permission;
      if (argument === 'status') { view.message(`YOLO: ${current === 'yolo' ? 'on' : 'off'} · approval mode: ${current}`); break; }
      const enabled = argument ? argument === 'on' : current !== 'yolo';
      await session.setPermission(enabled ? 'yolo' : 'manual');
      view.message(enabled
        ? 'YOLO enabled · routine actions are approved automatically. Use /yolo off to return to manual approval.'
        : 'YOLO disabled · manual approval restored.');
      break;
    }
    case '/permission': {
      if (argument) { await command(runtime, view, `/permissions ${argument}`); break; }
      const mode = await view.choose('Choose approval mode:', [
        { value: 'manual', label: 'Manual', description: 'Ask for actions that require approval' },
        { value: 'yolo', label: 'YOLO', description: 'Auto-approve routine tools; explicit denials and guarded checks still apply' },
        { value: 'auto', label: 'Never ask', description: 'Decide automatically and skip questions' },
      ]);
      if (!['manual', 'yolo', 'auto'].includes(mode)) break;
      const scope = await view.choose('Use this approval mode:', [{ value: 'session', label: 'This session only' }, { value: 'default', label: 'This session and new sessions', description: 'Save the default in config.toml; existing sessions keep their mode' }]);
      if (!scope) break;
      if (scope === 'default') await runtime.harness.setConfig({ defaultPermissionMode: mode as 'manual' | 'yolo' | 'auto' });
      await session.setPermission(mode as 'manual' | 'yolo' | 'auto'); view.message(`Permissions: ${mode}${scope === 'default' ? ' (saved as default for new sessions)' : ''}`);
      break;
    }
    case '/usage': case '/context': {
      const [context, usage, status] = await Promise.all([session.getContext(), session.getUsage(), session.getStatus()]);
      const total = usage.total;
      const byModel = Object.entries(usage.byModel ?? {}).map(([model, row]) => `${model}: input ${row.inputOther + row.inputCacheRead + row.inputCacheCreation} · output ${row.output}`);
      view.message(`${context.history.length} messages · context ${context.tokenCount}/${status.maxContextTokens} tokens${total ? `\nInput: ${total.inputOther} · cache read: ${total.inputCacheRead} · cache write: ${total.inputCacheCreation} · output: ${total.output}` : '\nNo token usage recorded.'}${byModel.length ? `\nBy model:\n${byModel.join('\n')}` : ''}`); break;
    }
    case '/compact': await session.compact({ instruction: argument || undefined }); view.message('Compaction requested'); break;
    case '/plan': {
      if (argument === 'show') { const plan = await session.getPlan(); if (!plan) view.message('No plan yet.'); else { view.message(`Plan${plan.path ? `: ${plan.path}` : ''}`); view.markdown(plan.content); } break; }
      if (argument === 'clear') { await session.clearPlan(); view.message('Plan cleared.'); break; }
      if (argument && argument !== 'on' && argument !== 'off') throw new Error('Usage: /plan [on|off|show|clear]');
      const enabled = argument ? argument === 'on' : !(await session.getStatus()).planMode; await session.setPlanMode(enabled); view.message(`Plan mode: ${enabled ? 'on' : 'off'}`); break;
    }
    case '/swarm': {
      const status = await session.getStatus();
      const sub = argument.toLowerCase();
      if (sub === 'status') { view.message(`Swarm mode: ${status.swarmMode ? 'on' : 'off'}`); break; }
      if (argument && sub !== 'on' && sub !== 'off') return false;
      const enabled = argument ? sub === 'on' : !status.swarmMode;
      if (enabled) { if (!await startSwarm(runtime, view, input, 'manual')) break; }
      else await session.setSwarmMode(false, 'manual');
      view.message(`Swarm mode: ${enabled ? 'on' : 'off'}`); break;
    }
    case '/tower': {
      if (!argument || argument === 'status') { view.message(await session.getTowerStatus()); break; }
      if (argument === 'teardown') return false;
      const [mode, ...base] = args;
      if (mode === 'off' && base.length) throw new Error('Usage: /tower [status|on [base]|off|teardown|base]');
      const enabled = mode !== 'off';
      await session.setTowerMode(enabled, mode === 'on' ? base.join(' ') || undefined : enabled ? argument : undefined);
      if (!!(await session.getStatus()).towerMode !== enabled) throw new Error('Tower mode could not be changed. Check /experiments, restart after enabling Tower, and check whether another session owns this workspace tower.');
      view.message(`Tower mode: ${enabled ? 'on' : 'off'}`); break;
    }
    case '/clear': await session.clearContext(); view.clearTranscript(); view.message('Context cleared'); break;
    case '/tasks': {
      const tasks = await session.listBackgroundTasks();
      if (view.agents.all().length) {
        const entries = [
          ...view.agents.all().map(agent => ({ value: `agent:${agent.agentId}`, label: agent.description || agent.name, description: `${agent.status} · ${agent.agentId}` })),
          ...tasks.filter(task => task.kind !== 'agent' || !task.agentId || !view.agents.get(task.agentId)).map(task => ({ value: `task:${task.taskId}`, label: task.description || task.taskId, description: task.status })),
        ];
        const selected = await view.choose('Choose task activity:', entries);
        if (selected.startsWith('agent:')) {
          const agentId = selected.slice(6);
          const task = tasks.find(task => task.kind === 'agent' && task.agentId === agentId);
          const action = await view.choose('Agent action:', [{ value: 'activity', label: 'Inspect live activity' }, ...(task ? [{ value: 'stop', label: 'Stop task' }] : [])]);
          if (action === 'activity') await view.showAgentActivity(agentId);
          if (action === 'stop' && task) { await session.stopBackgroundTask(task.taskId); view.message('Stop requested'); }
        } else if (selected.startsWith('task:')) {
          const id = selected.slice(5);
          const action = await view.choose('Task action:', [{ value: 'output', label: 'Show output' }, { value: 'stop', label: 'Stop task' }]);
          if (action === 'output') await view.showTaskOutput(session, id);
          if (action === 'stop') { await session.stopBackgroundTask(id); view.message('Stop requested'); }
        }
        break;
      }
      if (!tasks.length) { view.message('No background tasks.'); break; }
      const id = await view.choose('Choose a background task:', tasks.map(task => ({ value: task.taskId, label: task.description || task.taskId, description: `${task.status} · ${task.taskId}` })));
      if (!id) break;
      const action = await view.choose('Task action:', [{ value: 'output', label: 'Show output' }, { value: 'stop', label: 'Stop task' }]);
      if (action === 'output') await view.showTaskOutput(session, id);
      if (action === 'stop') { await session.stopBackgroundTask(id); view.message('Stop requested'); }
      break;
    }
    case '/stop': if (!argument) throw new Error('Usage: /stop <task-id>'); await session.stopBackgroundTask(argument); view.message('Stop requested'); break;
    case '/output': if (!argument) throw new Error('Usage: /output <task-id>'); await view.showTaskOutput(session, argument); break;
    case '/effort': if (!argument) { view.message(`Thinking effort: ${(await session.getStatus()).thinkingEffort}`); break; } await session.setThinking(argument); view.message(`Thinking effort: ${argument}`); break;
    case '/mcp': {
      if (args[0] === 'add' && args.length === 3) {
        const server = await readMcpConfiguration(args[1]!, args[2]!, session.workDir);
        const entry = await session.addMcpServer(server, { persist: true });
        view.message(`MCP server ${entry.name}: ${entry.status}${entry.error ? ` — ${entry.error}` : ''}. Saved in ~/.coderai/mcp.json.`); break;
      }
      if (args[0] === 'remove' && args.length === 2) {
        await removeMcpConfiguration(runtime, args[1]!);
        await session.removeMcpServer(args[1]!);
        view.message(`MCP server ${args[1]} removed and disconnected.`); break;
      }
      if (args[0] === 'test' && args.length === 2) { view.message(await testMcpConfiguration(runtime, args[1]!)); break; }
      if ((args[0] === 'auth' || args[0] === 'logout') && args[1] && args.length === 2) {
        if (args[0] === 'auth') await authenticateMcp(runtime, view, args[1]);
        else { await resetMcpAuthentication(runtime, args[1]); view.message(`MCP authorization removed: ${args[1]}`); }
        break;
      }
      if (args[0] === 'reconnect' && args[1] && args.length === 2) { await session.reconnectMcpServer(args[1]); view.message(`MCP reconnect requested: ${args[1]}`); break; }
      if (argument) throw new Error('Usage: /mcp [add NAME JSON-FILE|remove NAME|test NAME|auth NAME|logout NAME|reconnect NAME]');
      const servers = await session.listMcpServers();
      view.message(servers.length ? servers.map(server => `${server.name}: ${server.status} · ${server.toolCount} tools${server.error ? ` · ${server.error}` : ''}`).join('\n') : 'No MCP servers configured.'); break;
    }
    case '/skills': { const skills = session ? await session.listSkills() : await runtime.harness.listWorkspaceSkills(runtime.options.cwd); view.message(skills.length ? skills.map(skill => `${skill.name} (${skill.source})${skill.description ? ` — ${skill.description}` : ''}`).join('\n') : 'No skills available.'); break; }
    case '/plugins': await showPlugins(runtime, view, argument); break;
    case '/reload': await session.reloadSession(); view.onSessionReloaded?.(); await view.reloadPreferences(); view.message('Session configuration reloaded'); break;
    case '/todos': { const todos = await session.getTodos(); view.setTodos(todos); view.showTodos(todos, true); break; }
    case '/version': view.message(`CoderAI ${identity.version}`); break;
    case '/copy': { const method = await view.copyLastAssistant(); view.message(method === 'native' ? 'Clipboard: copied locally.' : method === 'osc52' ? 'Clipboard: request sent to terminal (OSC 52).' : 'No assistant message to copy.'); break; }
    case '/editor': if (!argument) throw new Error('Usage: /editor <command>'); await saveTuiConfig(runtime.harness.homeDir, { editor: { command: argument === 'default' ? '' : argument } }); await view.reloadPreferences(); view.message(`External editor: ${argument}`); break;
    case '/add-dir': {
      if (!argument || argument === 'list') { const dirs = session.summary?.additionalDirs ?? []; view.message(dirs.length ? `Additional directories:\n${dirs.join('\n')}` : 'No additional directories.'); break; }
      const persist = await view.choose('Remember this directory for the project?', [{ value: 'no', label: 'This session only' }, { value: 'yes', label: 'Remember for this project' }]);
      if (!persist) break;
      const path = input.trim().slice(name.length).trim().replace(/^"([\s\S]*)"$/, '$1');
      const result = await session.addAdditionalDir(path, { persist: persist === 'yes' });
      view.message(`Additional directory added${result.persisted ? ` and saved in ${result.configPath}` : ' for this session'}.\n${result.additionalDirs.join('\n')}`); break;
    }
    case '/init': await session.init(); view.message('Project instructions requested.'); break;
    case '/fork': { const fork = await runtime.harness.forkSession({ id: session.id, turnIndex: argument ? parseTurn(argument) : undefined }); view.message(`Forked session ${fork.id}`); await fork.close(); break; }
    case '/diff': {
      const result = await session.turnChanges(argument ? parseTurn(argument) : undefined);
      if (!result.changes.length) { view.message(`Turn ${result.turnId}: no recorded file changes. Snapshots cover the last five changed turns and edits made through Write/Edit tools.`); break; }
      const sections: string[] = [];
      for (const change of result.changes) {
        const diff = await session.turnFileDiff(result.turnId, change.path);
        sections.push(`${change.path} · ${change.status} · +${change.additions} -${change.deletions}\n` +
          (diff.unavailable ? 'Snapshot unavailable or too large to display.' : diff.binary ? 'Binary file changed.' :
            `--- ${change.status === 'added' ? '/dev/null' : change.path}\n+++ ${change.status === 'deleted' ? '/dev/null' : change.path}\n${unifiedDiff(diff.before ?? '', diff.after ?? '')}`));
      }
      await view.showDocument(`File changes · turn ${result.turnId}`, sections.join('\n\n')); break;
    }
    case '/undo': {
      const context = await session.getContext();
      const { prompts, stoppedAtCompaction } = undoablePrompts(context.history);
      if (!prompts.length) { view.message(stoppedAtCompaction ? 'Nothing to undo after the last compaction.' : 'Nothing to undo.'); break; }
      let count = Number(argument);
      if (!argument) {
        const choice = await view.choose('Withdraw prompts from context:', [...prompts].reverse().map((message, index) => {
          const text = typeof message.content === 'string' ? message.content : message.content.filter(part => part.type === 'text').map(part => part.type === 'text' ? part.text : '').join('');
          return { value: String(index + 1), label: text.replace(/\s+/g, ' ').slice(0, 100) || '(empty prompt)', description: `Withdraw ${index + 1} prompt${index ? 's' : ''}` };
        }));
        if (!choice) break;
        count = Number(choice);
      }
      if (!Number.isInteger(count) || count < 1 || count > prompts.length) throw new Error('Usage: /undo [positive count]');
      const selected = prompts[prompts.length - count]!;
      const draft = typeof selected.content === 'string' ? selected.content : selected.content.filter(part => part.type === 'text').map(part => part.type === 'text' ? part.text : '').join('');
      const mode = await view.choose(`Withdraw ${count} prompt${count === 1 ? '' : 's'}?`, [
        { value: 'prompts', label: 'Withdraw prompts only', description: 'Keep all current files' },
        { value: 'files', label: 'Withdraw prompts and revert file changes', description: 'Restore pre-turn Write/Edit snapshots, overwriting later edits to those files. Shell edits are not tracked.' },
      ]);
      if (!mode) break;
      await session.undoHistory(count, { revertFiles: mode === 'files' });
      view.clearTranscript(); view.message(`Withdrew ${count} prompt${count === 1 ? '' : 's'} from context; ${mode === 'files' ? 'tracked file changes reverted' : 'file changes remain'}.`);
      await replay(runtime, view); view.editor.setText(draft); break;
    }
    case '/export-md': {
      if (!argument) throw new Error('Usage: /export-md <path>');
      const path = resolve(session.workDir, argument);
      await exportMarkdown(session, path);
      view.message(`Exported conversation to ${path}`); break;
    }
    case '/export-debug': {
      const result = await runtime.harness.exportSession({ id: session.id, outputPath: argument ? resolve(session.workDir, argument) : undefined, version: identity.version });
      view.message(`Exported diagnostic archive to ${result.zipPath}`); break;
    }
    case '/theme': {
      const custom = await listThemes(runtime.harness.homeDir);
      const selected = argument || await view.choose('Choose a theme:', [{ value: 'auto', label: 'Auto' }, { value: 'dark', label: 'Dark terminal' }, { value: 'light', label: 'Light terminal' }, ...custom.map(name => ({ value: name, label: `Custom: ${name}` }))]);
      if (!selected) break;
      const restoreTheme = themeCheckpoint();
      if (!['auto', 'dark', 'light', ...custom].includes(selected) || !await setTheme(selected, runtime.harness.homeDir)) throw new Error(`Theme not found or invalid: ${selected}`);
      const temp = join(runtime.harness.homeDir, `.theme-${randomUUID()}`);
      try {
        await mkdir(runtime.harness.homeDir, { recursive: true, mode: 0o700 });
        await writeFile(temp, `${selected}\n`, { mode: 0o600, flag: 'wx' });
        await saveTuiConfig(runtime.harness.homeDir, { theme: selected });
        await rename(temp, join(runtime.harness.homeDir, 'theme'));
      } catch (error) { restoreTheme(); throw error; }
      finally { await unlink(temp).catch(() => {}); }
      await view.refreshTheme();
      view.tui.invalidate(); view.tui.requestRender(); view.message(`Theme: ${selected}`); break;
    }
    default: return false;
  }
  return true;
}
export async function replay(runtime: Runtime, view: TerminalView, durable = false): Promise<void> {
  const records = durable ? runtime.session!.getResumeState()?.agents['main']?.replay : undefined;
  if (records?.length) {
    for (const record of records) {
      if (record.type === 'message') {
        view.replayMessage(record.message);
      } else if (record.type === 'compaction') view.message(`Context compaction: ${record.result === 'cancelled' ? 'cancelled' : 'completed'}`);
      else if (record.type === 'plan_updated') view.message(`Plan mode: ${record.enabled ? 'on' : 'off'}`);
      else if (record.type === 'permission_updated') view.message(`Permissions: ${record.mode}`);
      else if (record.type === 'goal_updated') view.message(`Goal: ${JSON.stringify(record.snapshot)}`);
    }
    view.finishTurn(); return;
  }
  const context = await runtime.session!.getContext();
  for (const message of context.history) {
    view.replayMessage(message);
  }
  view.finishTurn();
}
