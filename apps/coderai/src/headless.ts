import type { Runtime } from './runtime.js';
import { sanitizeTerminalText, TerminalSanitizer } from './tui/sanitize.js';

export async function headless(runtime: Runtime, prompt: string, json: boolean, waitBackground = false): Promise<number> {
  const emit = (value: unknown) => process.stdout.write(`${JSON.stringify(value)}\n`);
  let denied = false;
  let failed = false;
  let cancelled = false;
  let cancelWait: (() => void) | undefined;
  let goalStatus: 'active' | 'paused' | 'blocked' | 'complete' | undefined;
  const terminalOutput = process.stdout.isTTY ? new TerminalSanitizer() : undefined;
  const stop = () => { cancelled = true; cancelWait?.(); void runtime.session?.cancel().catch(() => {}); };
  process.on('SIGINT', stop); process.on('SIGTERM', stop);
  try {
    const session = await runtime.open();
    if (cancelled) return 130;
    const attachments = runtime.options.attachments ?? [];
    const input = attachments.length ? await (await import('./attachments.js')).attachmentPrompt(runtime.harness, session, prompt, attachments, runtime.options.cwd) : undefined;
    const goalMatch = prompt.match(/^\/goal\s+([\s\S]+)$/);
    let actualPrompt = prompt;
    if (goalMatch) {
      let objective = goalMatch[1]!.trim();
      const replace = objective.startsWith('replace ');
      if (replace) objective = objective.slice(8).trim();
      else if (objective.startsWith('-- ')) objective = objective.slice(3).trim();
      else if (/^(status|pause|resume|cancel|next)(?:\s|$)/.test(objective)) throw new Error('Headless /goal only supports creating or replacing a goal.');
      if (!objective) throw new Error('Provide a goal objective.');
      if (input) throw new Error('Attachments are not supported with a headless /goal command.');
      await session.createGoal({ objective, replace });
      goalStatus = 'active'; actualPrompt = objective;
    }
    if (json) emit({ type: 'coderai.session', sessionId: session.id, workDir: session.workDir });
    else process.stderr.write(`Session: ${session.id}\n`);
    const reason = await runtime.run(actualPrompt, event => {
      if (event.type === 'error') failed = true;
      if (event.type === 'goal.updated' && event.snapshot) goalStatus = event.snapshot.status;
      if (json) emit(event);
      else if (event.type === 'assistant.delta' && event.agentId === 'main') process.stdout.write(terminalOutput ? terminalOutput.write(event.delta) : event.delta);
      else if (event.type === 'tool.call.started') process.stderr.write(`[tool] ${process.stderr.isTTY ? sanitizeTerminalText(event.name) : event.name}\n`);
      else if (event.type === 'error') { process.stderr.write(`Error: ${JSON.stringify(event)}\n`); }
    }, request => {
      denied = true;
      const result = { decision: 'rejected' as const, feedback: 'Headless mode denies actions requiring approval. Use --yolo to explicitly allow automatic approval.' };
      if (json) emit({ type: 'coderai.approval', toolCallId: request.toolCallId, toolName: request.toolName, decision: result.decision });
      const name = process.stderr.isTTY ? sanitizeTerminalText(request.toolName) : request.toolName;
      process.stderr.write(`Denied ${name}: approval requires an interactive terminal (or explicit --yolo).\n`);
      return result;
    }, undefined, input ? current => current.prompt(input) : undefined, !!goalMatch, true);
    if (waitBackground && !cancelled && reason === 'completed') {
      const off = session.onEvent(event => {
        if (json) emit(event);
        if (event.type === 'background.task.terminated' && event.info.status !== 'completed') failed = true;
      });
      try {
        const interrupted = new Promise<undefined>(resolve => { cancelWait = () => resolve(undefined); });
        // Bound the optional wait so unattended runs cannot hang indefinitely.
        const pending = await Promise.race([session.waitForBackgroundTasksOnPrint({ force: true, timeoutSeconds: 300 }), interrupted]);
        if (pending !== undefined && pending > 0) { failed = true; process.stderr.write(`Background wait timed out with ${pending} task(s) still running.\n`); }
      } finally { cancelWait = undefined; off(); }
    }
    const code = cancelled || reason === 'cancelled' ? 130 : failed || denied || reason !== 'completed' ? 1 : goalStatus === 'blocked' ? 3 : goalStatus === 'paused' ? 6 : goalStatus === 'active' ? 1 : 0;
    if (json) emit({ type: 'coderai.result', sessionId: session.id, reason, exitCode: code, ...(goalStatus ? { goalStatus } : {}) });
    else process.stdout.write('\n');
    return code;
  } finally { process.off('SIGINT', stop); process.off('SIGTERM', stop); }
}
