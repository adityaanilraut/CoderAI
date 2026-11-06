import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir, rm, chmod } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { setTimeout as delay } from 'node:timers/promises';
const { GitStatusPoller, parseGitStatus } = await import('../apps/coderai/src/tui/git-status.ts');
const { StatusFooter } = await import('../apps/coderai/src/tui/status.ts');
const { AttachmentPane } = await import('../apps/coderai/src/tui/attachments.ts');
const { inspectAttachment } = await import('../apps/coderai/src/attachment-info.ts');
const { TaskOutputViewer, watchTaskOutput, TASK_OUTPUT_LIMIT } = await import('../apps/coderai/src/tui/task-output.ts');
const { TerminalView } = await import('../apps/coderai/src/tui/view.ts');
const { setCapabilities } = await import('../packages/pi-tui/src/terminal-image.ts');
const { setTheme } = await import('../apps/coderai/src/tui/theme.ts');
const { WelcomePanel } = await import('../apps/coderai/src/tui/welcome.ts');
const { setupMenuLines } = await import('../apps/coderai/src/setup-terminal.ts');
const { colors, backgroundScheme, environmentScheme, loadTheme, refreshTerminalTheme, themeCheckpoint } = await import('../apps/coderai/src/tui/theme.ts');
const { stripTerminalSequences, visibleWidth } = await import('../packages/pi-tui/src/utils.ts');
const plain = lines => lines.map(stripTerminalSequences).join('\n');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/a1kAAAAASUVORK5CYII=', 'base64');
async function until(check) {
  const end = Date.now() + 5000;
  while (!check()) { if (Date.now() > end) throw new Error('Timed out'); await delay(10); }
}
async function root(t) {
  const dir = await mkdtemp(join(tmpdir(), 'coderai-workspace-ui-'));
  t.after(() => rm(dir, { recursive: true, force: true })); return dir;
}

test('Git parser handles unborn, tracked, untracked, detached and non-Git output', () => {
  assert.deepEqual(parseGitStatus('# branch.oid (initial)\0# branch.head feature\0'), { branch: 'feature', dirty: false });
  for (const entry of ['1 M. file', '2 R. renamed\0old', 'u UU conflict', '? untracked\nname']) {
    assert.equal(parseGitStatus('# branch.head main\0' + entry + '\0').dirty, true);
  }
  assert.deepEqual(parseGitStatus('# branch.oid abcdef123456\0# branch.head (detached)\0'), { branch: 'detached abcdef1', dirty: false });
  assert.equal(parseGitStatus('fatal: not a repository'), null);
});

test('Git cache refreshes real changes and branch switches, handles non-Git directories, and stops cleanly', async t => {
  const dir = await root(t), other = join(dir, 'other'); await mkdir(other);
  const git = (...args) => execFileSync('git', ['-C', dir, ...args], { stdio: 'pipe' });
  git('init', '-b', 'phase-three');
  let status, calls = 0;
  const poller = new GitStatusPoller(value => { status = value; calls++; }); t.after(() => poller.stop());
  poller.setWorkspace(dir); assert.equal(status, null);
  await until(() => status?.branch === 'phase-three'); assert.equal(status.dirty, false);
  await writeFile(join(dir, 'new.txt'), 'data'); poller.refresh();
  await until(() => status?.dirty);
  git('add', 'new.txt'); git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-m', 'fixture');
  git('checkout', '-b', 'changed'); poller.refresh();
  await until(() => status?.branch === 'changed'); assert.equal(status.dirty, false);
  poller.setWorkspace(other); // A directory within the repo remains a Git workspace.
  await until(() => status?.branch === 'changed');
  const nonGit = await root(t); const before = calls; poller.setWorkspace(nonGit);
  await until(() => calls >= before + 2); assert.equal(status, null);
  poller.setWorkspace(dir); poller.stop(); const stoppedCalls = calls;
  await delay(100); assert.equal(calls, stoppedCalls);
});

test('footer keeps branch and explicit dirty/clean state beside model and context at 50 columns', async () => {
  const footer = new StatusFooter();
  footer.setState({ permission: 'yolo', planMode: false, thinkingEffort: 'off', contextTokens: 20, maxContextTokens: 100 }, 'project', 'Example Model');
  for (const theme of ['dark', 'light']) {
    await setTheme(theme);
    for (const dirty of [true, false]) {
      footer.setGitStatus({ branch: 'feature/long-branch-name', dirty });
      const lines = footer.render(50);
      assert.equal(lines.length, 2); assert.ok(lines.every(line => visibleWidth(line) <= 50));
      assert.match(plain(lines), /git:feature\/.*(dirty|clean)/); assert.match(plain(lines), /Context 20%/);
    }
  }
  footer.setState({ permission: 'yolo', planMode: true, swarmMode: true, towerMode: true, thinkingEffort: 'off', contextTokens: 20, maxContextTokens: 100 }, 'project', 'Example Model');
  footer.setGitStatus({ branch: 'long-feature-name', dirty: true });
  assert.match(plain(footer.render(50)), /git:.*dirty/);
  footer.setGitStatus(null); assert.doesNotMatch(plain(footer.render(50)), /git:/);
  await setTheme('dark');
});

test('slow Git probes leave input responsive, coalesce refreshes and time out', { skip: process.platform === 'win32' }, async t => {
  const dir = await root(t), executable = join(dir, 'git');
  await writeFile(executable, '#!/bin/sh\necho probe >> "$0.calls"\nexec /bin/sleep 10\n'); await chmod(executable, 0o755);
  let updates = 0; const poller = new GitStatusPoller(() => updates++); t.after(() => poller.stop());
  const path = process.env.PATH;
  try { process.env.PATH = dir; poller.setWorkspace(dir); for (let i = 0; i < 20; i++) poller.refresh(); }
  finally { process.env.PATH = path; }
  let handled = false; setTimeout(() => { handled = true; }, 10);
  await delay(50); assert.equal(handled, true); assert.equal(updates, 1);
  await until(() => updates === 2);
  const { readFile } = await import('node:fs/promises');
  assert.equal((await readFile(executable + '.calls', 'utf8')).trim(), 'probe');
});

test('staged attachments sniff extensionless clipboard media, bound previews, and render inline or readable fallbacks', async t => {
  const dir = await root(t);
  await writeFile(join(dir, 'clipboard'), png);
  await writeFile(join(dir, 'video.mov'), Buffer.from([0, 0, 0, 16, 0x66, 0x74, 0x79, 0x70, 0x71, 0x74, 0x20, 0x20]));
  await writeFile(join(dir, 'broken.png'), 'invalid');
  const pane = new AttachmentPane(() => {}, () => 16);
  await pane.setPaths(['clipboard', 'video.mov', 'broken.png', 'missing.png'], dir);
  setCapabilities({ images: null, trueColor: true, hyperlinks: false });
  const fallback = pane.render(50);
  assert.match(plain(fallback), /clipboard \[image\/png\]/); assert.match(plain(fallback), /video.mov \[video\/quicktime\]/);
  assert.match(plain(fallback), /Ctrl\+T remove/); assert.match(plain(fallback), /\+2 more/);
  assert.match(plain(fallback), /Image preview unavailable/); assert.ok(fallback.length <= 6);
  assert.ok(fallback.every(line => visibleWidth(line) <= 50));
  for (const protocol of ['kitty', 'iterm2']) {
    setCapabilities({ images: protocol, trueColor: true, hyperlinks: false });
    const inline = pane.render(50).join('\n');
    assert.match(inline, protocol === 'kitty' ? /\x1b_G/ : /1337;File=/);
  }
  await pane.setPaths(['video.mov', 'clipboard'], dir);
  assert.match(pane.render(50).join('\n'), /1337;File=/);
  const bad = await inspectAttachment('broken.png', dir); assert.equal(bad.kind, 'unknown');
  assert.match((await inspectAttachment('missing.png', dir)).notice, /Unavailable/);
  assert.match((await inspectAttachment('.', dir)).notice, /regular file/);
  await writeFile(join(dir, 'large.png'), Buffer.concat([png, Buffer.alloc(4 * 1024 * 1024)]));
  assert.equal((await inspectAttachment('large.png', dir)).preview, undefined);
  await pane.setPaths([], dir); assert.deepEqual(pane.render(50), []);
  // A late inspection must not restore a removed file.
  const pending = pane.setPaths(['clipboard'], dir); await pane.setPaths([], dir); await pending;
  assert.deepEqual(pane.render(50), []);
  setCapabilities({ images: null, trueColor: true, hyperlinks: false });
});

test('task viewer pages, follows, freezes reading across ring rotation, resizes, and sanitizes output', () => {
  let rows = 16; const closed = [];
  const viewer = new TaskOutputViewer('task-1', () => rows, answer => closed.push(answer), () => {});
  const output = Array.from({ length: 70 }, (_, i) => `Log line ${i}`).join('\n');
  viewer.setOutput(output, 'running'); assert.match(plain(viewer.render(50)), /Log line 69/);
  viewer.handleInput('\x1b[5~'); const paused = plain(viewer.render(50)); assert.match(paused, /Paused/);
  viewer.setOutput(output + '\nNewest line', 'running');
  assert.match(plain(viewer.render(50)), /Paused · new output/); assert.doesNotMatch(plain(viewer.render(50)), /Newest line/);
  viewer.setOutput('Rotated ring\nTail', 'running');
  assert.match(plain(viewer.render(50)), /Log line 5/);
  viewer.handleInput('\x1b[H'); assert.match(plain(viewer.render(50)), /Log line 0/);
  viewer.handleInput('\x1b[6~'); assert.doesNotMatch(plain(viewer.render(50)), /Log line 0\n/);
  viewer.handleInput('\x1b[F'); assert.match(plain(viewer.render(50)), /Rotated ring/); assert.match(plain(viewer.render(50)), /Following/);
  viewer.setOutput('safe\x1b]52;c;ATTACK\x07\t' + 'wide 日 '.repeat(50), 'completed');
  for (const [width, height] of [[50, 16], [38, 12], [88, 32]]) {
    rows = height; const rendered = viewer.render(width);
    assert.ok(rendered.length <= height - 2); assert.ok(rendered.every(line => visibleWidth(line) <= width));
    assert.doesNotMatch(rendered.join('\n'), /ATTACK|52;c/);
  }
  viewer.setError(new Error('offline')); assert.match(plain(viewer.render(50)), /Refresh failed/);
  viewer.handleInput('\x1b[113u'); assert.deepEqual(closed, ['']);
});

test('task polling is sequential, bounded, recovers from errors, and ignores late replies after disposal', async () => {
  let calls = 0, active = 0, peak = 0, writes = 0, errors = 0, fail = true;
  const session = {
    async getBackgroundTaskOutput(_id, options) {
      assert.equal(options.tail, TASK_OUTPUT_LIMIT); calls++; peak = Math.max(peak, ++active);
      await delay(15); active--; if (fail) { fail = false; throw new Error('retry'); } return 'log';
    },
    async listBackgroundTasks() { return [{ taskId: 'one', status: 'completed' }]; },
  };
  const dispose = watchTaskOutput(session, 'one', { setOutput() { writes++; }, setError() { errors++; } }, 5);
  await until(() => writes > 0); await until(() => active > 0); dispose();
  const stopped = writes; await delay(50);
  assert.equal(peak, 1); assert.equal(errors, 1); assert.equal(writes, stopped); assert.ok(calls >= 3);
});

test('task output uses shared dialog lifecycle and restores the conversation draft on return', async t => {
  const dir = await root(t), view = new TerminalView(dir, dir, []);
  t.after(() => { view.stop(); });
  let card, hidden = false;
  view.tui.requestRender = () => {};
  view.tui.stop = () => {};
  view.tui.showOverlay = component => { card = component; return { hide() { hidden = true; } }; };
  view.editor.setText('Keep this draft 日');
  const opened = view.showTaskOutput({ async getBackgroundTaskOutput() { return 'large log\n'.repeat(80); }, async listBackgroundTasks() { return []; } }, 'one');
  await until(() => !!card);
  card.render(50); card.handleInput('\x1b[5~'); card.handleInput('\x1b'); await opened;
  assert.equal(view.editor.getText(), 'Keep this draft 日'); assert.equal(hidden, true);
  assert.doesNotMatch(plain(view.transcript.render(50)), /large log/);
  await view.flushHistory();
});

test('welcome and setup stay compact, actionable and readable across themes, long paths and resizes', async t => {
  const chalk = (await import('chalk')).default, level = chalk.level; chalk.level = 3; t.after(() => { chalk.level = level; });
  const welcome = new WelcomePanel();
  welcome.setWorkspace('/workspace/' + 'a very long directory/'.repeat(30) + 'project 日');
  for (const theme of ['light', 'dark']) {
    await setTheme(theme);
    for (const [width, rows] of [[110, 32], [50, 16], [38, 12], [50, 16], [110, 32]]) {
      const lines = welcome.render(width);
      assert.equal(lines.length, 4); assert.ok(lines.every(line => visibleWidth(line) <= width));
      assert.match(plain(lines), /project 日/); assert.match(plain(lines), /\/setup/);
      const menu = setupMenuLines({ title: 'Choose a provider', items: [...Array.from({ length: 30 }, (_, index) => ({ label: `Provider ${index}`, description: 'A long endpoint /'.repeat(30), index })), { label: 'Back', index: -1 }], selected: 20, query: '', shortcut: '', columns: width, rows });
      assert.ok(menu.length <= rows - 2); assert.ok(menu.every(line => visibleWidth(line) < width));
      assert.match(plain(menu), /❯ 21. Provider 20/); assert.match(plain(menu), /Esc back · Ctrl\+C exit/);
      assert.match(menu[0], /\x1b\[38;2;/);
    }
    welcome.setModel('Example model');
    assert.match(plain(welcome.render(50)), /Ready to code/);
    assert.match(plain(welcome.render(50)), /Example model/);
    const frame = new TaskOutputViewer('task-' + 'long'.repeat(20), () => 16, () => {}, () => {});
    frame.setOutput('long 日 line '.repeat(100), 'running');
    assert.ok(frame.render(50).every(line => visibleWidth(line) <= 50));
    welcome.setWorkspace('safe\x1b]52;c;ATTACK\x07/project');
    assert.doesNotMatch(welcome.render(50).join('\n'), /ATTACK/);
    welcome.setWorkspace('/workspace/' + 'long/'.repeat(30) + 'project 日');
  }
  await setTheme('dark');
});

test('background classification validates RGB and ANSI fallback values', () => {
  assert.equal(backgroundScheme({ r: 255, g: 255, b: 255 }), 'light');
  assert.equal(backgroundScheme({ r: 30, g: 30, b: 30 }), 'dark');
  assert.equal(backgroundScheme({ r: NaN, g: 0, b: 0 }), undefined);
  assert.equal(environmentScheme('0;15'), 'light'); assert.equal(environmentScheme('15;0'), 'dark');
  for (const value of ['bad', '0;16', '0;-1', '0;7junk', '0;']) assert.equal(environmentScheme(value), undefined);
});

test('terminal theme queries are bounded, respect explicit/custom choices and ignore stale replies', async t => {
  const dir = await root(t); let queries = 0, invalidated = 0;
  const host = { async queryTerminalBackgroundColor({ timeoutMs }) { queries++; assert.ok(timeoutMs <= 250); return { r: 255, g: 255, b: 255 }; }, invalidate() { invalidated++; }, requestRender() {} };
  await loadTheme(dir); await refreshTerminalTheme(host, dir);
  assert.equal(colors.primary, '#005A9C'); assert.equal(invalidated, 1);
  await setTheme('dark'); await refreshTerminalTheme(host, dir); assert.equal(queries, 1);
  await mkdir(join(dir, 'themes')); await writeFile(join(dir, 'themes', 'custom.json'), JSON.stringify({ name: 'Custom', base: 'light', colors: { primary: '#123456' } }));
  await setTheme('custom', dir); await refreshTerminalTheme(host, dir); assert.equal(colors.primary, '#123456'); assert.equal(queries, 1);
  const restore = themeCheckpoint(); await setTheme('auto'); restore(); await refreshTerminalTheme(host, dir); assert.equal(queries, 1);
  await setTheme('auto'); const palette = { ...colors };
  const start = performance.now();
  await refreshTerminalTheme({ ...host, queryTerminalBackgroundColor() { return new Promise(() => {}); } }, dir);
  assert.ok(performance.now() - start < 1000); assert.deepEqual(colors, palette);
  let reply;
  const pending = refreshTerminalTheme({ ...host, queryTerminalBackgroundColor() { return new Promise(resolve => { reply = resolve; }); } }, dir);
  await setTheme('custom', dir); reply({ r: 0, g: 0, b: 0 }); await pending;
  assert.equal(colors.primary, '#123456');
  await setTheme('auto'); let stopped = false;
  const closing = refreshTerminalTheme({ ...host, queryTerminalBackgroundColor() { return new Promise(resolve => { reply = resolve; }); } }, dir, () => !stopped);
  const before = { ...colors }; stopped = true; reply({ r: 0, g: 0, b: 0 }); await closing; assert.deepEqual(colors, before);
  await writeFile(join(dir, 'theme'), 'light\n'); await loadTheme(dir); assert.equal(colors.primary, '#005A9C');
  await writeFile(join(dir, 'theme'), 'missing\n'); await loadTheme(dir); assert.equal(colors.primary, '#61AFEF');
  await setTheme('dark');
});

test('workspace components and dialogs keep their bounds after resize and theme changes', async t => {
  const dir = await root(t); const path = 'a very long staged filename 日 '.repeat(3) + '.png'; await writeFile(join(dir, path), png);
  const { ChoiceDialog, DocumentDialog } = await import('../apps/coderai/src/tui/dialogs.ts');
  const { SessionPicker } = await import('../apps/coderai/src/tui/session-picker.ts');
  const pane = new AttachmentPane(() => {}, () => rows); await pane.setPaths([path, 'unavailable.mp4'], dir);
  setCapabilities({ images: null, trueColor: true, hyperlinks: false });
  const footer = new StatusFooter(); footer.setState({ contextTokens: 5, maxContextTokens: 100, permission: 'yolo', planMode: true }, dir, 'Long model name '.repeat(5));
  footer.setGitStatus({ branch: 'long/branch/'.repeat(15), dirty: true });
  let rows = 16;
  const choice = new ChoiceDialog('Plan\n' + 'A long plan 日\n'.repeat(80), [{ value: 'approve', label: 'Approve' }, { value: 'reject', label: 'Reject' }], () => rows, () => {}, () => {});
  const doc = new DocumentDialog('Help', 'Help text 日 '.repeat(400), () => rows, () => {}, () => {});
  const sessions = new SessionPicker([{ id: 'saved', title: 'A saved conversation', workDir: dir + '/long'.repeat(40), updatedAt: 1, createdAt: 1 }], { id: 'other', workDir: dir }, { all: true }, () => rows, () => {}, () => {});
  const output = new TaskOutputViewer('long-task-id-'.repeat(8), () => rows, () => {}, () => {}); output.setOutput('Output 日 '.repeat(400), 'running');
  for (const theme of ['light', 'dark']) {
    await setTheme(theme);
    for (const [width, height] of [[110, 32], [50, 16], [38, 12], [50, 16], [110, 32]]) {
      rows = height;
      for (const component of [pane, footer, choice, doc, sessions, output]) {
        component.invalidate(); const lines = component.render(width);
        assert.ok(lines.every(line => visibleWidth(line) <= width), component.constructor.name);
        assert.ok(lines.length <= rows - 2, component.constructor.name);
      }
      choice.handleInput('\x1b[6~'); output.handleInput('\x1b[5~');
    }
  }
  await setTheme('dark');
});

test('built-in semantic colors remain legible and code highlighting follows the active palette', async t => {
  const chalk = (await import('chalk')).default, level = chalk.level; chalk.level = 3; t.after(() => { chalk.level = level; });
  const { markdownTheme } = await import('../apps/coderai/src/tui/theme.ts');
  const luminance = hex => {
    const [r, g, b] = hex.match(/[a-f\d]{2}/gi).map(channel => { const value = parseInt(channel, 16) / 255; return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  let previous;
  for (const theme of ['light', 'dark']) {
    await setTheme(theme); const background = luminance(theme === 'light' ? '#ffffff' : '#1e1e1e');
    for (const color of Object.values(colors)) {
      const foreground = luminance(color); assert.ok((Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05) >= 4.5, `${theme} ${color}`);
    }
    const code = markdownTheme.highlightCode('const name = "value"; // comment', 'javascript').join('\n');
    assert.equal(stripTerminalSequences(code), 'const name = "value"; // comment');
    assert.ok(code.includes(chalk.hex(colors.primary)('const'))); assert.ok(code.includes(chalk.hex(colors.success)('"value"')));
    if (previous) assert.notEqual(code, previous); previous = code;
  }
  await setTheme('dark');
});

test('existing recovery notices adopt a changed theme without losing their text', async t => {
  const dir = await root(t), view = new TerminalView(dir, dir, []);
  const chalk = (await import('chalk')).default, level = chalk.level; chalk.level = 3;
  view.tui.requestRender = () => {}; view.tui.stop = () => {};
  t.after(async () => { chalk.level = level; view.stop(); await view.flushHistory(); });
  view.notice('Recoverable warning', 'Try /setup');
  let previous;
  for (const theme of ['dark', 'light']) {
    await setTheme(theme); view.transcript.invalidate();
    const rendered = view.transcript.render(50).join('\n');
    assert.ok(rendered.includes(chalk.hex(colors.warning)('Recoverable warning')));
    assert.match(stripTerminalSequences(rendered), /Try \/setup/);
    if (previous) assert.notEqual(rendered, previous); previous = rendered;
  }
  await setTheme('dark');
});
