/**
 * Selectable slash commands for the interactive /help menu.
 * Keep in sync with handleSlashCommand in useAgent.ts.
 */
export type HelpMenuEntry = {
  /** Shown in the first column; sent on Enter. */
  slash: string;
  /** Short description (single line; truncated in UI if needed) */
  desc: string;
};

export const HELP_MENU_ENTRIES: HelpMenuEntry[] = [
  {slash: "/help", desc: "Open this command menu"},
  {slash: "/version", desc: "CoderAI version string"},
  // Model — keep this group together near the top
  {
    slash: "/change-model",
    desc: "Change model — lists providers; add <name> to switch (/change-model <name>)",
  },
  {slash: "/models", desc: "List models & providers (same list as /change-model)"},
  {
    slash: "/model",
    desc: "Session model: /model <name> to switch · bare /model = show current",
  },
  {
    slash: "/default",
    desc: "Saved default for new chats: /default <name>",
  },
  {slash: "/clear", desc: "Wipe conversation & context"},
  {slash: "/compact", desc: "Summarize long context"},
  {slash: "/reasoning", desc: "Thinking effort — usage: /reasoning <high|medium|low|none>"},
  {slash: "/yolo", desc: "Toggle auto-approve for high-risk tools"},
  {slash: "/status", desc: "Tokens, cost, context (status bar)"},
  {slash: "/cost", desc: "Budget & reference pricing"},
  {slash: "/system", desc: "Keys, endpoints, paths (like coderAI status)"},
  {slash: "/config", desc: "Effective config (API keys masked)"},
  {slash: "/info", desc: "Version, model, tool list"},
  {slash: "/tasks", desc: "Project tasks (.coderAI/tasks.json)"},
  {slash: "/plan", desc: "Current execution plan (current_plan.json)"},
  {slash: "/agents", desc: "Note about agents table & live updates"},
  {slash: "/exit", desc: "Shut down the agent"},
];

/** One line for the menu footer (CLI commands need a normal shell). */
export const HELP_CLI_FOOTER =
  "CLI only (exit chat): coderAI setup · coderAI config set <k> <v> · coderAI history list · …";
