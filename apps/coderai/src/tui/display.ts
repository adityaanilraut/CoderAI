import type { ToolInputDisplay } from '@coderai/sdk';
import { previewDiff } from './diff.js';

export function describeGoal(goal: { objective: string; status: string; turnsUsed: number; tokensUsed: number } | null | undefined): string {
  return goal ? `${goal.objective}\nStatus: ${goal.status} · turns ${goal.turnsUsed} · tokens ${goal.tokensUsed}` : 'No active goal.';
}

export function describeMcp(server: { name: string; status: string; toolCount: number; error?: string }): string {
  return `${server.name}: ${server.status} · ${server.toolCount} tools${server.error ? ` · ${server.error}` : ''}`;
}

export function describeTask(task: { taskId: string; description: string; status: string }): string {
  return `${task.description || task.taskId} · ${task.status} · ${task.taskId}`;
}

export function describeToolInput(display: ToolInputDisplay): string {
  switch (display.kind) {
    case 'command': return [display.description, display.cwd && `Directory: ${display.cwd}`, `$ ${display.command}`].filter(Boolean).join('\n');
    case 'file_io': return [ `${display.operation}: ${display.path}`, display.detail, display.before !== undefined && display.after !== undefined ? previewDiff(display.before, display.after) : display.content ].filter(value => value !== undefined && value !== '').join('\n');
    case 'diff': return `${display.path}\n${previewDiff(display.before, display.after)}`;
    case 'search': return `Search: ${display.query}${display.scope ? `\nScope: ${display.scope}` : ''}`;
    case 'url_fetch': return `${display.method ?? 'GET'} ${display.url}`;
    case 'agent_call': return `Agent: ${display.agent_name}${display.background ? ' (background)' : ''}\n${display.prompt}`;
    case 'skill_call': return `Skill: ${display.skill_name}${display.args ? `\n${display.args}` : ''}`;
    case 'todo_list': return display.items.map(item => `[${item.status}] ${item.title}`).join('\n');
    case 'task': return `Task ${display.task_id}: ${display.status}\n${display.description}`;
    case 'task_stop': return `Stop ${display.task_id}: ${display.task_description}`;
    case 'plan_review': return `${display.path ? `Plan: ${display.path}\n` : ''}${display.plan}`;
    case 'goal_start': return `Goal: ${display.objective}${display.completionCriterion ? `\nCompletion: ${display.completionCriterion}` : ''}\nMode: ${display.mode}`;
    case 'generic': return `${display.summary}${display.detail === undefined ? '' : `\n${typeof display.detail === 'string' ? display.detail : JSON.stringify(display.detail, null, 2)}`}`;
  }
}
