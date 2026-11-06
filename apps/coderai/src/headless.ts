import { inlineSkillActivations } from './inline-skills.js';
import { eventNotice, McpAuthorizationOpener } from './event-format.js';
import type { Event } from '@coderai/sdk';
import type { Runtime } from './runtime.js';
import { sanitizeTerminalText, TerminalSanitizer } from './tui/sanitize.js';

export async function headless(runtime: Runtime, prompt: string, json: boolean, waitBackground = false): Promise<number> {
  const emit = (value: unknown) => process.stdout.write(`${JSON.stringify(value)}\n`);
  const abort = new AbortController();
  let denied = false;
  let failed = false;
  let cancelled = false;
  let cancelWait: (() => void) | undefined;
  let goalStatus: 'active' | 'paused' | 'blocked' | 'complete' | undefined;
  const terminalOutput = process.stdout.isTTY ? new TerminalSanitizer() : undefined;
  const diagnostic = (text: string) => process.stderr.write((process.stderr.isTTY ? sanitizeTerminalText(text) : text) + '\n');
  const oauth = new McpAuthorizationOpener(diagnostic);
  const diagnostics = (event: Event) => {
    const notice = eventNotice(event);
    if (notice) diagnostic(notice);
    switch (event.type) {
      case 'tool.call.started': diagnostic(`[tool] ${event.name}`); break;
      case 'tool.call.delta': if (event.name || event.argumentsPart) diagnostic(`[tool args] ${event.name ?? event.toolCallId}: ${event.argumentsPart ?? ''}`); break;
      case 'tool.progress': oauth.handle(event); if (event.update.text) diagnostic(event.update.text); break;
      case 'session.meta.updated': { const title = event.title ?? event.patch?.['title']; if (typeof title === 'string') diagnostic(`Session title: ${title}`); break; }
      case 'skill.activated': diagnostic(`Skill activated: ${event.skillName}`); break;
      case 'subagent.spawned': diagnostic(`Agent ${event.subagentId} · ${event.subagentName} · started`); break;
      case 'subagent.completed': diagnostic(`Agent ${event.subagentId} · completed · ${event.resultSummary}`); break;
      case 'subagent.failed': diagnostic(`Agent ${event.subagentId} · failed · ${event.error}`); break;
      case 'error': diagnostic(`Error: ${event.message}`); break;
    }
  };
  const stop = () => { cancelled = true; abort.abort(); cancelWait?.(); void runtime.session?.cancel().catch(() => {}); };
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
  try {
    const session = await runtime.open();
    if (cancelled) return 130;
    session.setStaleReloadHandler(async () => {
      if (json) emit({ type: 'coderai.session.reloaded', sessionId: session.id, title: session.summary?.title });
      else diagnostic('Session changed on disk; reloaded the latest history.');
      await showWarnings();
    });
    const showWarnings = async () => {
      try {
        for (const warning of await session.getSessionWarnings()) {
          if (json) emit({ type: 'warning', sessionId: session.id, agentId: 'main', ...warning });
          else diagnostic(`Warning: ${warning.message}`);
        }
      } catch { /* Warning retrieval must not block a run. */ }
    };
    const attachments = runtime.options.attachments ?? [];
    const goalMatch = prompt.trim().match(/^\/goal(?:\s+([\s\S]+))?$/);
    let actualPrompt = prompt;
    let goalRequest: { objective: string; replace: boolean } | undefined;
    if (goalMatch) {
      let objective = goalMatch[1]?.trim() ?? 'status';
      const control = objective.match(/^(status|pause|resume|cancel)$/)?.[1];
      if (control) {
        if (attachments.length) throw new Error('Attachments are only supported when creating a goal or sending a prompt.');
        if (control !== 'resume') {
          if (control === 'pause') await session.pauseGoal();
          if (control === 'cancel') await session.cancelGoal();
          const { goal } = await session.getGoal();
          if (json) emit({ type: 'goal.summary', sessionId: session.id, goal });
          else diagnostic(goal ? `Goal: ${goal.objective}\nStatus: ${goal.status} · turns ${goal.turnsUsed} · tokens ${goal.tokensUsed}` : 'No active goal.');
          return 0;
        }
        const goal = await session.resumeGoal();
        goalStatus = goal.status; actualPrompt = 'Resume the active goal.';
        objective = '';
      }
      const replace = /^replace(?:\s|$)/.test(objective);
      if (replace) objective = objective.slice(7).trim();
      if (/^--(?:\s|$)/.test(objective)) objective = objective.slice(2).trim();
      else if (/^next(?:\s|$)/.test(objective)) throw new Error('Headless goal queues are not supported. Use /goal <objective> or lifecycle controls.');
      if (!control) {
        if (!objective) throw new Error('Provide a goal objective.');
        goalRequest = { objective, replace }; actualPrompt = objective;
      }
    }
    let dispatch: ((current: typeof session) => Promise<void>) | undefined;
    const invocation = !goalMatch ? prompt.trim().match(/^\/(\S+)(?:\s+([\s\S]*))?$/) : null;
    let skill: string | undefined;
    let plugin: { pluginId: string; name: string } | undefined;
    if (invocation) {
      const name = invocation[1]!;
      const args = invocation[2] ?? '';
      if (name.startsWith('skill:')) {
        skill = name.slice(6);
        const skills = await session.listSkills();
        if (!skills.some(item => item.name === skill && (!item.scopes || item.scopes.includes('tui')))) throw new Error(`Skill unavailable in headless mode: ${skill}`);
        actualPrompt = args || `Use the ${skill} skill.`;
      } else if (name.includes(':')) {
        plugin = (await session.listPluginCommands()).find(item => `${item.pluginId}:${item.name}` === name);
        if (!plugin) throw new Error(`Plugin command not found: /${name}`);
        actualPrompt = args;
      }
    }
    const input = attachments.length ? await (await import('./attachments.js')).attachmentPrompt(runtime.harness, session, actualPrompt, attachments, runtime.options.cwd, plugin ? 'references' : 'parts') : undefined;
    const availableSkills = /\s\/[^\s/]+/.test(prompt) ? await session.listSkills() : [];
    const inline = inlineSkillActivations(prompt, new Map(availableSkills.filter(item => !item.scopes || item.scopes.includes('tui')).map(item => [`skill:${item.name}`, item.name])));
    if (skill) { const name = skill; dispatch = current => current.promptWithSkills(input ?? actualPrompt, [{ name, args: invocation?.[2] || undefined }, ...inline.filter(item => item.name !== name)]); }
    else if (plugin) { const { pluginId, name } = plugin; const args = input ? input.filter(part => part.type === 'text').map(part => part.text).join('\n\n') : actualPrompt; dispatch = current => current.activatePluginCommand(pluginId, name, args || undefined); }
    else if (inline.length) dispatch = current => current.promptWithSkills(input ?? actualPrompt, inline);
    else if (input) dispatch = current => current.prompt(input);
    if (goalRequest) { await session.createGoal(goalRequest); goalStatus = 'active'; }
    if (json) emit({ type: 'coderai.session', sessionId: session.id, workDir: session.workDir });
    else process.stderr.write(`Session: ${session.id}\n`);
    await showWarnings();
    const reason = await runtime.run(actualPrompt, event => {
      if (event.type === 'background.task.terminated' && ['failed', 'timed_out'].includes(event.info.status)) failed = true;
      if (event.type === 'error' && event.agentId === 'main') failed = true;
      if (event.type === 'goal.updated' && event.snapshot) goalStatus = event.snapshot.status;
      if (json) emit(event);
      else if (event.type === 'assistant.delta' && event.agentId === 'main') process.stdout.write(terminalOutput ? terminalOutput.write(event.delta) : event.delta);
      else diagnostics(event);
    }, request => {
      denied = true;
      const result = { decision: 'rejected' as const, feedback: 'Headless mode denies actions requiring approval. Use --yolo to explicitly allow automatic approval.' };
      if (json) emit({ type: 'coderai.approval', toolCallId: request.toolCallId, toolName: request.toolName, decision: result.decision });
      const name = process.stderr.isTTY ? sanitizeTerminalText(request.toolName) : request.toolName;
      process.stderr.write(`Denied ${name}: approval requires an interactive terminal (or explicit --yolo).\n`);
      return result;
    }, undefined, dispatch, !!goalMatch, true, !waitBackground, abort.signal);
    if (waitBackground && !cancelled && reason === 'completed') {
      const off = session.onEvent(event => {
        if (json) emit(event); else diagnostics(event);
        if (event.type === 'background.task.terminated' && ['failed', 'timed_out'].includes(event.info.status)) failed = true;
      });
      try {
        const interrupted = new Promise<undefined>(resolve => { cancelWait = () => resolve(undefined); });
        // Bound the optional wait so unattended runs cannot hang indefinitely.
        const pending = await Promise.race([session.waitForBackgroundTasksOnPrint({ force: true, timeoutSeconds: 300 }), interrupted]);
        if (pending !== undefined && pending > 0) { failed = true; process.stderr.write(`Background wait timed out with ${pending} task(s) still running.\n`); }
      } finally { cancelWait = undefined; off(); }
    }
    if (reason === 'background_timeout') diagnostic('Background continuation timed out before the next main turn completed.');
    const code = cancelled || reason === 'cancelled' ? 130 : failed || denied || reason !== 'completed' ? 1 : goalStatus === 'blocked' ? 3 : goalStatus === 'paused' ? 6 : goalStatus === 'active' ? 1 : 0;
    if (json) emit({ type: 'coderai.result', sessionId: session.id, reason, exitCode: code, ...(goalStatus ? { goalStatus } : {}) });
    else process.stdout.write('\n');
    return code;
  } finally { if (runtime.session && !runtime.session.isClosed) runtime.session.setStaleReloadHandler(undefined); process.off('SIGINT', stop); process.off('SIGTERM', stop); }
}
