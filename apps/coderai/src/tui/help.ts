const groups = [
  { title: 'CONVERSATIONS', commands: ['help', 'new', 'sessions', 'resume', 'import-sessions', 'import', 'title', 'fork', 'undo', 'quit'] },
  { title: 'MODELS & SETTINGS', commands: ['model', 'secondary-model', 'subagent-model', 'settings', 'config', 'reload-tui', 'experiments', 'experimental', 'doctor', 'setup', 'provider', 'effort', 'permission', 'permissions', 'yolo', 'theme', 'editor', 'auto-title', 'reload', 'version'] },
  { title: 'WORK & PLANNING', commands: ['plan', 'goal', 'todos', 'tasks', 'stop', 'output', 'queue', 'steer', 'btw', 'swarm', 'tower'] },
  { title: 'CONTEXT & FILES', commands: ['status', 'context', 'usage', 'compact', 'clear', 'diff', 'attach', 'add-dir', 'init', 'copy', 'export-md', 'export-debug'] },
  { title: 'EXTENSIONS', commands: ['skills', 'plugins', 'mcp', 'agents', 'hooks', 'cron'] },
];

export function groupedHelp(help: string): string {
  const lines = help.split('\n');
  const commands = new Map(lines.filter(line => line.startsWith('/')).map(line => [line.match(/^\/(\S+)/)![1]!, line]));
  const sections = groups.map(group => [group.title, ...group.commands.flatMap(name => {
    const line = commands.get(name); commands.delete(name); return line ? [line] : [];
  })].join('\n'));
  if (commands.size) sections.push('MORE COMMANDS\n' + [...commands.values()].join('\n'));
  return ['KEYBOARD SHORTCUTS\n' + lines.filter(line => line && !line.startsWith('/')).join('\n'), ...sections].join('\n\n');
}
