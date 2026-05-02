/**
 * Central theme tokens — CoderAI Design System.
 *
 * Two palettes ship: a Tokyo Night dark theme (default) and a paper-light
 * theme for light terminals. Pick at startup with `CODERAI_THEME=light`. The
 * shape is identical so components don't need conditional logic — they just
 * import `theme` and read tokens.
 *
 * Design thesis: "quiet rails, loud signals". Borders are expensive
 * cognitively — a left-edge colored rail groups a block with a tenth the
 * visual weight. Colors are earned: muted by default, semantic colors only
 * when something genuinely needs attention.
 */

interface ThemeShape {
  accent: string;
  accentDim: string;
  success: string;
  warning: string;
  danger: string;
  info: string;
  muted: string;
  faint: string;
  text: string;
  textSoft: string;
  bg: string;
  bgElevated: string;
  link: string;
  codeBlock: {bg: string; label: string};
  diff: {addBg: string; delBg: string};
  tool: {
    filesystem: string;
    git: string;
    terminal: string;
    web: string;
    search: string;
    memory: string;
    agent: string;
    mcp: string;
    other: string;
  };
  risk: {low: string; medium: string; high: string};
  role: {user: string; assistant: string; system: string};
  spacing: {sm: number; md: number};
  layout: {narrowCols: number};
  glyph: {
    separator: string;
    dot: string;
    bullet: string;
    diamond: string;
    caret: string;
    sentCaret: string;
    arrowRun: string;
    rail: string;
    tick: string;
    cross: string;
    warn: string;
    wait: string;
    cog: string;
    pulse: string;
    info: string;
    cancelled: string;
  };
}

const dark: ThemeShape = {
  accent: "#7aa2f7",
  accentDim: "#3d59a1",

  success: "#9ece6a",
  warning: "#e0af68",
  danger: "#f7768e",
  info: "#7dcfff",
  muted: "#565f89",
  faint: "#525985",

  text: "#c0caf5",
  textSoft: "#a9b1d6",

  bg: "#1a1b26",
  bgElevated: "#24283b",
  link: "#bb9af7",
  codeBlock: {bg: "#16161e", label: "#565f89"},

  diff: {
    addBg: "#1d3a1d",
    delBg: "#3a1d1d",
  },

  tool: {
    filesystem: "#7dcfff",
    git: "#e0af68",
    terminal: "#f7768e",
    web: "#bb9af7",
    search: "#7aa2f7",
    memory: "#ad8ee6",
    agent: "#9ece6a",
    mcp: "#ff9e64",
    other: "#a9b1d6",
  },

  risk: {
    low: "#9ece6a",
    medium: "#e0af68",
    high: "#f7768e",
  },

  role: {
    user: "#9ece6a",
    assistant: "#7aa2f7",
    system: "#bb9af7",
  },

  spacing: {sm: 1, md: 2},

  layout: {narrowCols: 72},

  glyph: {
    separator: "  ",
    dot: "·",
    bullet: "●",
    diamond: "◆",
    caret: "❯",
    sentCaret: "›",
    arrowRun: "→",
    rail: "▌",
    tick: "✓",
    cross: "✗",
    warn: "⚠",
    wait: "⏸",
    cog: "⚙",
    pulse: "◇",
    info: "ℹ",
    cancelled: "⊘",
  },
};

/**
 * Light variant — paper background, GitHub-Light-style accents. Keeps the
 * same semantic mapping as dark (success=green, danger=red, warning=amber)
 * but pushes saturation down and contrast up so the colors read on white.
 * Glyphs and spacing are shared.
 */
const light: ThemeShape = {
  ...dark,

  accent: "#0969da",
  accentDim: "#54aeff",

  success: "#1a7f37",
  warning: "#9a6700",
  danger: "#cf222e",
  info: "#0550ae",
  muted: "#57606a",
  faint: "#8c959f",

  text: "#1f2328",
  textSoft: "#424a53",

  bg: "#ffffff",
  bgElevated: "#f6f8fa",
  link: "#0550ae",
  codeBlock: {bg: "#f3f4f6", label: "#8c959f"},

  diff: {
    addBg: "#dafbe1",
    delBg: "#ffebe9",
  },

  tool: {
    filesystem: "#0550ae",
    git: "#9a6700",
    terminal: "#cf222e",
    web: "#8250df",
    search: "#0969da",
    memory: "#7c4dbd",
    agent: "#1a7f37",
    mcp: "#bc4c00",
    other: "#57606a",
  },

  risk: {
    low: "#1a7f37",
    medium: "#9a6700",
    high: "#cf222e",
  },

  role: {
    user: "#1a7f37",
    assistant: "#0969da",
    system: "#8250df",
  },
};

function resolveVariant(): ThemeShape {
  const requested = process.env.CODERAI_THEME?.toLowerCase();
  if (requested === "light") return light;
  // COLORFGBG is set by some terminals as "fg;bg" (e.g. "0;15" = black on
  // white). When bg index is 7 or 15 (white-ish), fall back to light. Cheap
  // best-effort detection — a one-liner override via CODERAI_THEME beats
  // any heuristic.
  const colorFgBg = process.env.COLORFGBG;
  if (colorFgBg && requested !== "dark") {
    const parts = colorFgBg.split(";");
    const bg = Number(parts[parts.length - 1]);
    if (bg === 7 || bg === 15) return light;
  }
  return dark;
}

export const theme = resolveVariant();
