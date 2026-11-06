/** Host callbacks shared by Markdown blocks without depending on TerminalView. */
export const markdownTerminal: { fullscreen: boolean; render?: () => void } = { fullscreen: false };
