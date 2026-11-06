import { ProcessTerminal, TuiMainScreen } from '@coderai/pi-tui';
import type { Runtime } from '../runtime.js';
import type { TerminalView } from '../tui/view.js';
import { importBudget, importSessions, scanImportableSessions, sourceHomes, type ImportReport } from './run-import.js';
import { ImportSessionsScreenComponent } from './screen.js';

function screenOptions(runtime: Runtime) {
  const now = Date.now();
  const coderaiHome = runtime.harness.homeDir;
  return {
    now,
    scan: () => scanImportableSessions({ coderaiHome, ...sourceHomes(), now }),
    run: async (candidates: Parameters<typeof importSessions>[0], hooks: Pick<Parameters<typeof importSessions>[1], 'onProgress' | 'shouldStop'>) => {
      const budget = importBudget(await runtime.harness.getConfig());
      const report = await importSessions(candidates, { coderaiHome, ...budget, now, ...hooks });
      // Refresh the live index now, so /sessions can see imports immediately.
      await Promise.all(report.imported.map(sessionId => runtime.harness.listSessions({ sessionId }).catch(() => [])));
      return report;
    },
  };
}

export async function showImportSessions(runtime: Runtime, view: TerminalView): Promise<void> {
  let screen: ImportSessionsScreenComponent;
  let report: ImportReport | undefined;
  await view.showDialog(finish => {
    screen = new ImportSessionsScreenComponent({ ...screenOptions(runtime),
      requestRender: () => view.tui.requestRender(),
      onClose: result => { report = result; finish(''); },
    });
    return screen;
  }, 'Import sessions · Esc cancel', {
    onCancel: data => screen.handleInput(data),
    onDismiss: () => screen.dispose(),
  });
  if (report?.imported.length) view.message(`Imported ${report.imported.length} sessions. Find them with /sessions (Ctrl+A shows all projects).`);
}

/** Standalone import page: no model, workspace trust or conversation required. */
export async function runImportSessionsCommand(runtime: Runtime): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('coderai import-sessions needs an interactive terminal.');
  const ui = new TuiMainScreen(new ProcessTerminal());
  let screen: ImportSessionsScreenComponent | undefined;
  try {
    await new Promise<void>(resolve => {
      screen = new ImportSessionsScreenComponent({ ...screenOptions(runtime), requestRender: () => ui.requestRender(), onClose: () => resolve() });
      ui.addChild(screen); ui.setFocus(screen); ui.start();
    });
  } finally { screen?.dispose(); ui.stop(); }
}
