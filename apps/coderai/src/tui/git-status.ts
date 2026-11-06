import { execFile, type ChildProcess } from 'node:child_process';

export type GitStatus = { branch: string; dirty: boolean };

export function parseGitStatus(output: string): GitStatus | null {
  const records = output.split('\0');
  const head = records.find(line => line.startsWith('# branch.head '))?.slice(14);
  if (!head) return null;
  const oid = records.find(line => line.startsWith('# branch.oid '))?.slice(13);
  return {
    branch: head === '(detached)' ? `detached ${oid?.slice(0, 7) || 'HEAD'}` : head,
    dirty: records.some(line => /^(1 |2 |u |\? )/.test(line)),
  };
}

/** Rendering reads only cached state; probes are bounded and never overlap. */
export class GitStatusPoller {
  private cwd = '';
  private version = 0;
  private child?: ChildProcess;
  private timer?: ReturnType<typeof setInterval>;
  constructor(private readonly publish: (status: GitStatus | null) => void) {}

  setWorkspace(cwd: string): void {
    if (cwd === this.cwd) { this.refresh(); return; }
    this.stop(); this.cwd = cwd; this.publish(null); this.refresh();
    this.timer = setInterval(() => this.refresh(), 5000); this.timer.unref();
  }

  refresh(): void {
    if (!this.cwd || this.child) return;
    const version = this.version;
    this.child = execFile('git', ['--no-optional-locks', '-C', this.cwd, 'status', '--porcelain=v2', '--branch', '-z', '--untracked-files=normal'],
      { encoding: 'utf8', timeout: 2000, maxBuffer: 2 * 1024 * 1024, windowsHide: true }, (error, output) => {
        if (version !== this.version) return;
        this.child = undefined; this.publish(error ? null : parseGitStatus(output));
      });
  }

  stop(): void {
    this.version++; this.cwd = '';
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined; this.child?.kill(); this.child = undefined;
  }
}
