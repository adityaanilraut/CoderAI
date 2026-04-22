/**
 * Central theme tokens so we don't sprinkle hex codes across components.
 * Ink uses the `chalk` color vocabulary; truecolor is supported as hex.
 */

export const theme = {
  // Brand palette for the terminal UI: warm editorial accent + cool operator tones.
  accent: "#d97757",
  accentDim: "#8f4d39",
  accentSoft: "#f2c4b4",
  success: "#81b29a",
  warning: "#d9a441",
  danger: "#d16d5b",
  info: "#6ea8c7",
  muted: "#7f8a96",
  text: "#e7dccf",
  border: "#73574d",
  borderSoft: "#4c5660",
  focus: "#edd4ae",

  // Tool category colors (match Python `_TOOL_CATEGORIES`)
  tool: {
    fs: "#6ea8c7",
    git: "#d9a441",
    shell: "#d16d5b",
    web: "#d97757",
    search: "#8ab6a2",
    agent: "#81b29a",
    mcp: "#c99054",
    other: "#a5adb8",
  },

  // Risk badges
  risk: {
    low: "#81b29a",
    medium: "#d9a441",
    high: "#d16d5b",
  },

  // Backgrounds for diff viewer
  diff: {
    addBg: "#183126",
    delBg: "#3a221f",
  },
} as const;
