import { writeFile } from 'node:fs/promises';
import type { Session } from '@coderai/sdk';

/** Export the durable replay rather than the model context, which can be compacted or cleared. */
export async function exportMarkdown(session: Session, path: string): Promise<void> {
  const state = await session.getPersistedReplay();
  const sections: string[] = [`# CoderAI conversation\n\nSession: ${session.id}\n\nWorkspace: ${session.workDir}`];
  for (const [agentId, agent] of Object.entries(state.agents)) {
    if (agentId !== 'main') sections.push(`## Agent ${agentId}`);
    for (const record of agent.replay) {
      if (record.type === 'message') {
        const message = record.message;
        if (message.role === 'system' || message.origin?.kind === 'injection') continue;
        const content = typeof message.content === 'string' ? message.content : message.content.map(part => {
          if (part.type === 'text') return part.text;
          if (part.type === 'think') return part.hidden ? '' : part.think;
          return `[${part.type} attachment]`;
        }).join('');
        const label = message.role === 'user' ? 'You' : message.role === 'assistant' ? 'CoderAI' : `Tool ${message.isError ? 'failure' : 'result'}`;
        if (content) sections.push(`## ${label}\n\n${content}`);
        for (const call of message.toolCalls ?? []) {
          const args = call.arguments ?? '';
          // Choose a fence longer than any run in the data so tool output cannot close it.
          const fence = '`'.repeat(Math.max(3, ...Array.from(args.matchAll(/`+/g), match => match[0].length + 1)));
          sections.push(`### Tool call: ${call.name}\n\n${fence}\n${args}\n${fence}`);
        }
      } else if (record.type === 'compaction') {
        sections.push(`## Context compaction\n\n${record.result === 'cancelled' ? 'Cancelled' : 'Completed'}${record.instruction ? `\n\n${record.instruction}` : ''}`);
      } else if (record.type === 'plan_updated') sections.push(`## Plan mode\n\n${record.enabled ? 'On' : 'Off'}`);
      else if (record.type === 'permission_updated') sections.push(`## Permission mode\n\n${record.mode}`);
      else if (record.type === 'goal_updated') sections.push(`## Goal update\n\n${JSON.stringify(record.snapshot, null, 2)}`);
    }
  }
  await writeFile(path, `${sections.join('\n\n')}\n`, { flag: 'wx', mode: 0o600 });
}
