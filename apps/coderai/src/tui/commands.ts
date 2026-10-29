import { undoablePrompts } from './undo.js';
import { identity, type Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { latestSessionId } from '../session-selection.js';
import { mkdir, rename, unlink, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { colors, listThemes, setTheme } from './theme.js';
import { exportMarkdown } from '../session-export.js';
import { chooseModel } from './model-picker.js';

export const help = `/help                         Show commands and keyboard shortcuts
/new                          New saved conversation
/sessions                     Browse and resume conversations
/resume [id|latest]           Resume a conversation
/title [title]                Show or set the session title
/status                       Show session and runtime status
/btw [question]               Ask a read-only side question
/model [alias]                Choose a provider and model, or select an alias
/permissions [manual|yolo|auto] Inspect or change approvals
/permission                   Choose approval mode
/yolo [on|off|status]         Toggle automatic approval
/context                      Show context usage
/usage                        Show token usage
/compact [instruction]        Compact context through the model
/clear                        Clear conversation context
/plan [on|off|show|clear]     Control plan mode or the current plan
/swarm [on|off]               Inspect or change swarm mode
/tower [status|on|off]        Inspect or change tower mode
/goal <objective|status|pause|resume|cancel|next> Manage an autonomous goal
/tasks                        List background tasks
/stop <task-id>                Stop a background task
/output <task-id>              Show background task output
/effort <level>                Set thinking effort
/mcp                          Show MCP server status
/skills                       List available skills
/plugins [action]             Inspect or manage plugins
/reload                       Reload the session configuration
/todos                        Show the current task list
/version                      Show CoderAI version
/add-dir [path|list]          List or add workspace directories
/init                         Generate project instructions
/fork                         Fork this conversation
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
Ctrl+G external editor · Ctrl+O expand tool output · Ctrl+S steer
Up on empty input recalls queued work · Esc cancel work · Esc twice undo
Ctrl+C cancel or quit · Ctrl+D quit (twice while work is running)`;

export async function command(runtime: Runtime, view: TerminalView, input: string): Promise<boolean> {
  const [name, ...args] = input.split(/\s+/); const argument = args.join(' ');
  const session = runtime.session!;
  switch (name) {
    case '/help': view.message(help); break;
    case '/new': await runtime.open(''); view.clearTranscript(); view.message(`New session ${runtime.session!.id}`); break;
    case '/sessions': case '/resume':
      { let selected = argument;
        if (!selected) {
          const sessions = await runtime.harness.listSessions();
          if (!sessions.length) { view.message('No saved conversations.'); break; }
          selected = await view.chooseSearchable('Choose a conversation:', sessions.map(s => ({ value: s.id, label: s.title ?? '(untitled)', description: `${s.id} · ${s.workDir}` })));
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
    case '/status': { const status = await session.getStatus(); view.message(`Session: ${session.id}\nWorkspace: ${session.workDir}\nModel: ${status.model ?? '(none)'}\nPermissions: ${status.permission}\nPlan mode: ${status.planMode ? 'on' : 'off'}\nContext: ${status.contextTokens}/${status.maxContextTokens} tokens`); break; }
    case '/model':
      if (argument) { await runtime.validateModel(argument); await session.setModel(argument); view.message(`Model: ${argument}`); }
      else await chooseModel(runtime, view);
      break;
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
      const mode = await view.choose('Choose approval mode:', [
        { value: 'manual', label: 'Manual', description: 'Ask for actions that require approval' },
        { value: 'yolo', label: 'YOLO', description: 'Auto-approve routine actions; explicit denials and guarded checks still apply' },
        { value: 'auto', label: 'Never ask', description: 'Allow and decide actions automatically' },
      ]);
      if (mode === 'manual' || mode === 'yolo' || mode === 'auto') { await session.setPermission(mode); view.message(`Permissions: ${mode}`); }
      break;
    }
    case '/usage': case '/context': {
      const [context, usage, status] = await Promise.all([session.getContext(), session.getUsage(), session.getStatus()]);
      const total = usage.total;
      view.message(`${context.history.length} messages · context ${context.tokenCount}/${status.maxContextTokens} tokens${total ? `\nInput: ${total.inputOther} · cache read: ${total.inputCacheRead} · cache write: ${total.inputCacheCreation} · output: ${total.output}` : '\nNo token usage recorded.'}`); break;
    }
    case '/compact': await session.compact({ instruction: argument || undefined }); view.message('Compaction requested'); break;
    case '/plan': {
      if (argument === 'show') { const plan = await session.getPlan(); if (!plan) view.message('No plan yet.'); else { view.message(`Plan${plan.path ? `: ${plan.path}` : ''}`); view.markdown(plan.content); } break; }
      if (argument === 'clear') { await session.clearPlan(); view.message('Plan cleared.'); break; }
      if (argument && argument !== 'on' && argument !== 'off') throw new Error('Usage: /plan [on|off|show|clear]');
      const enabled = argument ? argument === 'on' : !(await session.getStatus()).planMode; await session.setPlanMode(enabled); view.message(`Plan mode: ${enabled ? 'on' : 'off'}`); break;
    }
    case '/swarm': {
      if (!argument || argument === 'status') { view.message(`Swarm mode: ${(await session.getStatus()).swarmMode ? 'on' : 'off'}`); break; }
      if (argument !== 'on' && argument !== 'off') throw new Error('Usage: /swarm [on|off|status]');
      await session.setSwarmMode(argument === 'on', 'manual'); view.message(`Swarm mode: ${argument}`); break;
    }
    case '/tower': {
      if (!argument || argument === 'status') { view.message(await session.getTowerStatus()); break; }
      const [mode, ...base] = args;
      if (mode !== 'on' && mode !== 'off') throw new Error('Usage: /tower [status|on [base]|off]');
      await session.setTowerMode(mode === 'on', base.join(' ') || undefined); view.message(`Tower mode: ${mode}`); break;
    }
    case '/clear': await session.clearContext(); view.clearTranscript(); view.message('Context cleared'); break;
    case '/tasks': {
      const tasks = await session.listBackgroundTasks();
      if (!tasks.length) { view.message('No background tasks.'); break; }
      const id = await view.choose('Choose a background task:', tasks.map(task => ({ value: task.taskId, label: task.description || task.taskId, description: `${task.status} · ${task.taskId}` })));
      if (!id) break;
      const action = await view.choose('Task action:', [{ value: 'output', label: 'Show output' }, { value: 'stop', label: 'Stop task' }]);
      if (action === 'output') view.message((await session.getBackgroundTaskOutput(id)) || '(No output)');
      if (action === 'stop') { await session.stopBackgroundTask(id); view.message('Stop requested'); }
      break;
    }
    case '/stop': if (!argument) throw new Error('Usage: /stop <task-id>'); await session.stopBackgroundTask(argument); view.message('Stop requested'); break;
    case '/output': if (!argument) throw new Error('Usage: /output <task-id>'); view.message(await session.getBackgroundTaskOutput(argument)); break;
    case '/effort': if (!argument) { view.message(`Thinking effort: ${(await session.getStatus()).thinkingEffort}`); break; } await session.setThinking(argument); view.message(`Thinking effort: ${argument}`); break;
    case '/mcp': {
      if (args[0] === 'reconnect' && args[1]) { await session.reconnectMcpServer(args[1]); view.message(`MCP reconnect requested: ${args[1]}`); break; }
      if (argument) throw new Error('Usage: /mcp [reconnect NAME]');
      const servers = await session.listMcpServers();
      view.message(servers.length ? servers.map(server => `${server.name}: ${server.status} · ${server.toolCount} tools${server.error ? ` · ${server.error}` : ''}`).join('\n') : 'No MCP servers configured.'); break;
    }
    case '/skills': { const skills = await session.listSkills(); view.message(skills.length ? skills.map(skill => `${skill.name} (${skill.source})${skill.description ? ` — ${skill.description}` : ''}`).join('\n') : 'No skills available.'); break; }
    case '/plugins': {
      const [action, ...parts] = args; const target = parts.join(' ');
      if (!action || action === 'list') { const plugins = await session.listPlugins(); view.message(plugins.length ? plugins.map(plugin => `${plugin.displayName} (${plugin.id}) · ${plugin.enabled ? 'enabled' : 'disabled'} · ${plugin.state} · ${plugin.skillCount} skills · ${plugin.enabledMcpServerCount}/${plugin.mcpServerCount} MCP servers`).join('\n') : 'No plugins installed.'); break; }
      if (action === 'reload') { await session.reloadPlugins(); view.message('Plugins reloaded.'); break; }
      if (!target) throw new Error('Usage: /plugins list|reload|install SOURCE|enable ID|disable ID|remove ID');
      if (action === 'install') { await session.installPlugin(target); view.message(`Plugin installed: ${target}`); }
      else if (action === 'enable' || action === 'disable') { await session.setPluginEnabled(target, action === 'enable'); view.message(`Plugin ${target} ${action}d`); }
      else if (action === 'remove') { await session.removePlugin(target); view.message(`Plugin ${target} removed`); }
      else throw new Error('Usage: /plugins list|reload|install SOURCE|enable ID|disable ID|remove ID');
      break;
    }
    case '/reload': await session.reloadSession(); view.message('Session configuration reloaded'); break;
    case '/todos': { const todos = await session.getTodos(); view.message(todos.length ? todos.map(todo => `[${todo.status}] ${todo.title}`).join('\n') : 'No todos.'); break; }
    case '/version': view.message(`CoderAI ${identity.version}`); break;
    case '/copy': view.message(view.copyLastAssistant() ? 'Clipboard request sent.' : 'No assistant message to copy.'); break;
    case '/editor': if (!argument) throw new Error('Usage: /editor <command>'); view.setEditorCommand(argument); view.message(`External editor: ${argument}`); break;
    case '/add-dir': {
      if (!argument || argument === 'list') { const dirs = session.summary?.additionalDirs ?? []; view.message(dirs.length ? `Additional directories:\n${dirs.join('\n')}` : 'No additional directories.'); break; }
      const persist = await view.choose('Remember this directory for the project?', [{ value: 'no', label: 'This session only' }, { value: 'yes', label: 'Remember for this project' }]);
      if (!persist) break;
      const result = await session.addAdditionalDir(argument, { persist: persist === 'yes' });
      view.message(`Additional directory added${result.persisted ? ` and saved in ${result.configPath}` : ' for this session'}.\n${result.additionalDirs.join('\n')}`); break;
    }
    case '/init': await session.init(); view.message('Project instructions requested.'); break;
    case '/fork': { const fork = await runtime.harness.forkSession({ id: session.id }); view.message(`Forked session ${fork.id}`); await fork.close(); break; }
    case '/undo': {
      const context = await session.getContext();
      const { prompts, stoppedAtCompaction } = undoablePrompts(context.history);
      if (!prompts.length) { view.message(stoppedAtCompaction ? 'Nothing to undo after the last compaction.' : 'Nothing to undo.'); break; }
      let count = Number(argument);
      if (!argument) {
        const choice = await view.choose('Withdraw prompts from context (file changes remain):', [...prompts].reverse().map((message, index) => {
          const text = typeof message.content === 'string' ? message.content : message.content.filter(part => part.type === 'text').map(part => part.type === 'text' ? part.text : '').join('');
          return { value: String(index + 1), label: text.replace(/\s+/g, ' ').slice(0, 100) || '(empty prompt)', description: `Withdraw ${index + 1} prompt${index ? 's' : ''}` };
        }));
        if (!choice) break;
        count = Number(choice);
      }
      if (!Number.isInteger(count) || count < 1 || count > prompts.length) throw new Error('Usage: /undo [positive count]');
      const selected = prompts[prompts.length - count]!;
      const draft = typeof selected.content === 'string' ? selected.content : selected.content.filter(part => part.type === 'text').map(part => part.type === 'text' ? part.text : '').join('');
      await session.undoHistory(count); view.clearTranscript(); view.message(`Withdrew ${count} prompt${count === 1 ? '' : 's'} from context; file changes remain.`); await replay(runtime, view); view.editor.setText(draft); break;
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
      const previousColors = { ...colors };
      if (!['auto', 'dark', 'light', ...custom].includes(selected) || !await setTheme(selected, runtime.harness.homeDir)) throw new Error(`Theme not found or invalid: ${selected}`);
      const temp = join(runtime.harness.homeDir, `.theme-${randomUUID()}`);
      try {
        await mkdir(runtime.harness.homeDir, { recursive: true, mode: 0o700 });
        await writeFile(temp, `${selected}\n`, { mode: 0o600, flag: 'wx' });
        await rename(temp, join(runtime.harness.homeDir, 'theme'));
      } catch (error) { Object.assign(colors, previousColors); throw error; }
      finally { await unlink(temp).catch(() => {}); }
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
        const message = record.message;
        if (message.role === 'system' || message.origin?.kind === 'injection') continue;
        const text = typeof message.content === 'string' ? message.content : message.content.map(part => {
          if (part.type === 'text') return part.text;
          if (part.type === 'think') return part.hidden ? '' : `Thinking: ${part.think}`;
          return `[${part.type} attachment]`;
        }).join('');
        if (text) {
          view.message(message.role === 'user' ? 'You:' : message.role === 'assistant' ? 'CoderAI:' : `Tool ${message.isError ? 'failed' : 'result'}:`);
          view.markdown(text);
          if (message.role === 'assistant') view.setLastAssistantText(typeof message.content === 'string' ? message.content : message.content.filter(part => part.type === 'text').map(part => part.type === 'text' ? part.text : '').join(''));
        }
        for (const call of message.toolCalls ?? []) view.message(`Tool: ${call.name}\n${call.arguments ?? ''}`);
      } else if (record.type === 'compaction') view.message(`Context compaction: ${record.result === 'cancelled' ? 'cancelled' : 'completed'}`);
      else if (record.type === 'plan_updated') view.message(`Plan mode: ${record.enabled ? 'on' : 'off'}`);
      else if (record.type === 'permission_updated') view.message(`Permissions: ${record.mode}`);
      else if (record.type === 'goal_updated') view.message(`Goal: ${JSON.stringify(record.snapshot)}`);
    }
    return;
  }
  const context = await runtime.session!.getContext();
  for (const message of context.history) {
    if (message.role !== 'user' && message.role !== 'assistant') continue;
    const text = typeof message.content === 'string' ? message.content : message.content?.filter(p => p.type === 'text').map(p => p.type === 'text' ? p.text : '').join('');
    if (text) { view.message(message.role === 'user' ? 'You:' : 'CoderAI:'); view.markdown(text); if (message.role === 'assistant') view.setLastAssistantText(text); }
  }
}
