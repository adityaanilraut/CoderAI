import type { Runtime } from '../runtime.js';
import type { TerminalView } from './view.js';
import { loadTuiConfig, saveTuiConfig, STATUS_LINE_ITEMS, type TuiConfigPatch } from './config.js';

export async function showSettings(runtime: Runtime, view: TerminalView): Promise<string | undefined> {
  const config = await loadTuiConfig(runtime.harness.homeDir, text => view.notice(text));
  const selected = await view.chooseSearchable('Settings', [
    { value: '/model', label: 'Model' }, { value: '/secondary-model', label: 'Subagent model' },
    { value: '/permission', label: 'Permissions and default approval mode' }, { value: '/theme', label: `Theme: ${config.theme}` },
    { value: '/experiments', label: 'Experimental features' },
    { value: 'auto_session_title', label: `Automatic session titles: ${config.autoSessionTitle ? 'on' : 'off'}` },
    { value: 'render_latex', label: `LaTeX rendering: ${config.renderLatex ? 'on' : 'off'}` },
    { value: 'disable_paste_burst', label: `Paste burst detection: ${config.disablePasteBurst ? 'off' : 'on'}` },
    { value: 'notifications', label: `Notifications: ${config.notifications.enabled ? config.notifications.condition : 'off'}` },
    { value: 'editor', label: `External editor: ${config.editorCommand || '$VISUAL / $EDITOR'}` },
    { value: 'status_line', label: 'Status line slots and command' },
    { value: 'tui_mode', label: `TUI mode: ${config.tuiMode}`, description: 'Transcript search, scrolling and mouse support; restart to change modes' },
    { value: 'mermaid', label: `Mermaid preference: ${config.markdown.mermaid}`, description: 'Draw completed diagrams in the terminal; source fallback when unsupported or too wide' },
  ]);
  if (!selected) return;
  if (selected.startsWith('/')) return selected;
  let patch: TuiConfigPatch = {};
  if (['auto_session_title', 'render_latex', 'disable_paste_burst'].includes(selected)) {
    const key = selected as 'auto_session_title' | 'render_latex' | 'disable_paste_burst';
    const current = key === 'auto_session_title' ? config.autoSessionTitle : key === 'render_latex' ? config.renderLatex : config.disablePasteBurst;
    patch = { [key]: !current };
  } else if (selected === 'editor') {
    const value = await view.ask('External editor command (default uses $VISUAL / $EDITOR):', 'Enter save · Esc cancel', config.editorCommand);
    // PromptDialog returns an empty string for both empty input and cancellation; an explicit reset is unambiguous.
    if (!value.trim()) return;
    patch = { editor: { command: value === 'default' ? '' : value } };
  } else if (selected === 'notifications') {
    const value = await view.choose('Terminal notifications', ['off', 'unfocused', 'always'].map(value => ({ value, label: value })));
    if (!value) return;
    patch = { notifications: { enabled: value !== 'off', notification_condition: value === 'always' ? 'always' : 'unfocused' } };
  } else if (selected === 'status_line') {
    const value = await view.choose('Status line', [{ value: 'items', label: 'Choose built-in slots' }, { value: 'command', label: 'Set custom command', description: 'Receives JSON on stdin; first stdout line replaces the status line' }, { value: 'default', label: 'Restore default layout' }]);
    if (!value) return;
    if (value === 'default') patch = { status_line: { items: [...STATUS_LINE_ITEMS], command: '' } };
    else if (value === 'command') {
      const command = await view.ask('Status command (default clears it):', 'Enter save · Esc cancel', config.statusLine.command);
      if (!command.trim()) return; patch = { status_line: { command: command === 'default' ? '' : command } };
    } else {
      const items = await view.chooseMultiple('Status slots (in displayed order):', STATUS_LINE_ITEMS.map(label => ({ label })), '');
      if (!items) return;
      patch = { status_line: { items: items.filter(item => (STATUS_LINE_ITEMS as readonly string[]).includes(item)), command: '' } };
    }
  } else {
    const choices = selected === 'tui_mode' ? ['regular', 'fullscreen'] : ['off', 'final'];
    const value = await view.choose(selected === 'tui_mode' ? 'TUI mode preference' : 'Mermaid preference', choices.map(value => ({ value, label: value })));
    if (!value) return; patch = selected === 'tui_mode' ? { tui_mode: value } : { markdown: { mermaid: value } };
  }
  await saveTuiConfig(runtime.harness.homeDir, patch);
  await view.reloadPreferences(); view.message('TUI preferences saved and reloaded.');
}
