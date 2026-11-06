import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';

export const TOWER_TEARDOWN_PROMPT = 'Tear down the tower: call TowerTeardown and report what it did. It refuses to destroy dirty worktrees unless forced.';
export async function startSwarm(runtime: Runtime, view: TerminalView, command: string, trigger: 'manual' | 'task'): Promise<boolean> {
  const session = runtime.session!;
  const status = await session.getStatus();
  if (status.permission === 'manual') {
    const choice = await view.choose('Start Swarm with approvals on?', [
      { value: 'auto', label: 'Switch to Never Ask and start', description: 'Automatically decide actions and skip questions' },
      { value: 'yolo', label: 'Switch to YOLO and start', description: 'Automatically approve routine tools; questions may still appear' },
      { value: 'manual', label: 'Keep manual approval and start', description: 'Swarm may wait for your approval while agents run' },
    ]);
    if (!choice) { view.editor.setText(command); view.message('Swarm not started. Command restored.'); return false; }
    if (choice === 'auto' || choice === 'yolo') await session.setPermission(choice);
  }
  if (!status.swarmMode) await session.setSwarmMode(true, trigger);
  if (!(await session.getStatus()).swarmMode) throw new Error('Swarm mode could not be enabled.');
  return true;
}

export function swarmTask(input: string): string | undefined {
  const match = input.match(/^\/swarm\s+([\s\S]+)$/);
  const task = match?.[1]?.trim();
  return task && !['on', 'off', 'status'].includes(task.toLowerCase()) ? task : undefined;
}
