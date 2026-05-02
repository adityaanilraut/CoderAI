/**
 * Selectable slash commands for the interactive /help menu.
 * Keep in sync with handleSlashCommand in useAgent.ts.
 *
 * The menu only lists the everyday commands. Reference / debug
 * topics live behind `/show <topic>` so they don't clutter the picker
 * but stay reachable.
 */
export type HelpMenuEntry = {
  /** Shown in the first column; sent on Enter. */
  slash: string;
  /** Short description (single line; truncated in UI if needed) */
  desc: string;
};

export const HELP_MENU_ENTRIES: HelpMenuEntry[] = [
  {slash: "/help", desc: "Open this command menu"},
  {slash: "/model", desc: "Open model picker · /model <name> · /model default <name>"},
  {slash: "/plan", desc: "Show current execution plan"},
  {slash: "/clear", desc: "Wipe conversation & context"},
  {slash: "/compact", desc: "Summarize long context"},
  {slash: "/reasoning", desc: "Open reasoning picker · /reasoning <high|medium|low|none>"},
  {slash: "/yolo", desc: "Toggle auto-approve for high-risk tools"},
  {slash: "/allow-tool", desc: "Always allow one tool for this session"},
  {slash: "/disallow-tool", desc: "Remove a per-session tool allowlist entry"},
  {slash: "/allowed-tools", desc: "List tools already allowlisted this session"},
  {slash: "/verbose", desc: "Toggle reasoning + expanded tool cards"},
  {slash: "/agents", desc: "Refresh the agents panel"},
  {slash: "/show", desc: "Reference info · type /show then a topic"},
  {slash: "/think", desc: "Reveal the latest hidden reasoning as a toast"},
  {slash: "/search", desc: "Search conversation transcript · /search <query>"},
  {slash: "/export", desc: "Export session to markdown · /export [path]"},
  {slash: "/copy", desc: "Copy last assistant response to clipboard (OSC-52)"},
  {slash: "/theme", desc: "Set dark/light theme · /theme <dark|light>"},
  {slash: "/undo", desc: "Undo last tool action · /undo [count]"},
  {slash: "/tokens", desc: "Show token usage, cost & context stats"},
  {slash: "/exit", desc: "Shut down the agent (type twice to confirm)"},
];

/** One line for the menu footer (CLI commands need a normal shell). */
export const HELP_CLI_FOOTER =
  "CLI only (exit chat): coderAI setup · coderAI config · coderAI models · coderAI cost · coderAI status · coderAI doctor · coderAI history · coderAI tasks · coderAI info · coderAI set-model";
