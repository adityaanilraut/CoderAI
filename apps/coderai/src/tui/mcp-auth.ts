import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { authenticateMcpConfiguration } from '../mcp-operations.js';
import { openBrowser } from '../browser.js';

export async function authenticateMcp(runtime: Runtime, view: TerminalView, name: string, open = openBrowser): Promise<void> {
  const controller = new AbortController();
  let dialog: Promise<void> | undefined;
  let finished = false;
  try {
    await authenticateMcpConfiguration(runtime, name, {
      signal: controller.signal,
      open,
      onUrl: url => {
        dialog = view.showDocument(`Authorize ${name}`, `Complete sign-in in your browser. If it did not open, use this URL:\n\n${url}\n\nClose this dialog to cancel.`);
        void dialog.then(() => { if (!finished) controller.abort(); });
      },
    });
    view.message(`MCP server ${name} authorized.`);
  } catch (error) {
    if (controller.signal.aborted) view.message('MCP authorization cancelled.');
    else throw error;
  } finally {
    finished = true;
    if (dialog) { view.dismissDialog(); await dialog; }
  }
}
