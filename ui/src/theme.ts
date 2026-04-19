/**
 * Central theme tokens so we don't sprinkle hex codes across components.
 * Ink uses the `chalk` color vocabulary; truecolor is supported as hex.
 */

export const theme = {
  // Accent / brand
  accent: "#7aa2f7",
  accentDim: "#3d59a1",
  success: "#9ece6a",
  warning: "#e0af68",
  danger: "#f7768e",
  info: "#7dcfff",
  muted: "#565f89",

  // Tool category colors (match Python `_TOOL_CATEGORIES`)
  tool: {
    fs: "#7dcfff",
    git: "#e0af68",
    shell: "#f7768e",
    web: "#bb9af7",
    search: "#7aa2f7",
    agent: "#9ece6a",
    mcp: "#ff9e64",
    other: "#a9b1d6",
  },

  // Risk badges
  risk: {
    low: "#9ece6a",
    medium: "#e0af68",
    high: "#f7768e",
  },

  // Backgrounds for diff viewer
  diff: {
    addBg: "#1d3a1d",
    delBg: "#3a1d1d",
    hunkBg: "#1a1b26",
  },
} as const;

export type ToolCategoryColor = keyof typeof theme.tool;
