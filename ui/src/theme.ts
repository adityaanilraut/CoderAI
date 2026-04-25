/**
 * Central theme tokens — CoderAI Design System (Tokyo Night palette).
 * Ink uses the `chalk` color vocabulary; truecolor is supported as hex.
 *
 * Design thesis: "quiet rails, loud signals".  Borders are expensive
 * cognitively — a left-edge colored rail groups a block with a tenth the
 * visual weight.  Colors are earned: muted by default, semantic colors
 * only when something genuinely needs attention.
 */

export const theme = {
  // Brand / Accent — Tokyo Night blues
  accent: "#7aa2f7",      // Primary blue — headers, cursor, active states
  accentDim: "#3d59a1",   // Dimmed blue — quiet borders, reasoning block

  // Semantic states
  success: "#9ece6a",     // Green — user bubble, ✓ icons, safe/low-risk
  warning: "#e0af68",     // Amber — git tools, ⚠ approval, YOLO mode
  danger: "#f7768e",      // Pink-red — shell tools, ✗ errors, high-risk
  info: "#7dcfff",        // Cyan — fs tools, help menu, waiting state
  muted: "#565f89",       // Slate — secondary text, quiet borders
  faint: "#414868",       // Darker slate — tertiary chrome, dividers

  // Main text (Tokyo Night foreground)
  text: "#c0caf5",
  textSoft: "#a9b1d6",    // Slightly dimmed primary text for body copy

  // Surfaces
  bg: "#1a1b26",          // App background
  bgElevated: "#24283b",  // Emphasis surface — selected rows, dialog headers

  // Diff viewer backgrounds
  diff: {
    addBg: "#1d3a1d",
    delBg: "#3a1d1d",
  },

  // Tool category colors — used for left rails on tool blocks
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

  // Risk badge colors
  risk: {
    low: "#9ece6a",
    medium: "#e0af68",
    high: "#f7768e",
  },

  // Chat role identity — rail + label color per participant
  role: {
    user: "#9ece6a",       // Green — the human
    assistant: "#7aa2f7",  // Blue — Claude
    system: "#bb9af7",     // Violet — tool/notices
  },

  spacing: {
    sm: 1,
    md: 2,
  },

  // Shared layout breakpoints. Components that adapt to width should read
  // `theme.layout.narrowCols` rather than redeclaring their own threshold.
  layout: {
    narrowCols: 72,
  },

  glyph: {
    separator: "  ",           // Two-space field separator (no more `│`)
    dot: "·",                  // Inline metadata separator
    bullet: "●",               // Generic marker
    diamond: "◆",              // Brand/agent mark
    caret: "❯",                // Live prompt
    sentCaret: "›",             // Echoed user message in transcript
    arrowRun: "→",             // Progress/hint arrow
    rail: "▌",                 // Left rail glyph (display only; rails use Ink borders)
    tick: "✓",
    cross: "✗",
    warn: "⚠",
    wait: "⏸",
    cog: "⚙",
    pulse: "◇",
    info: "ℹ",
    cancelled: "⊘",
  },
} as const;
