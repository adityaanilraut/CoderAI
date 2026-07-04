"""Design tokens for the CoderAI Textual UI.

Palette: **Tokyo Night** (https://github.com/folke/tokyonight.nvim) — the
CoderAI design system's mandated theme. Colors are *earned*: the UI defaults to
muted/faint tones and promotes to a semantic color only when something genuinely
needs attention.

Hex equivalents are kept in one place so widgets, modal screens, and Rich Text
styles all agree on the palette and glyph language. The existing token *names*
(``BG``, ``TEXT_DIM``, ``AGENT``, …) are preserved for compatibility with the
rest of the TUI.
"""

from __future__ import annotations


class Tokens:
    # --- Surfaces (Tokyo Night) ---
    BG = "#1a1b26"  # app background — deep navy
    BG_RAISED = "#24283b"  # elevated surface — selected rows, dialog headers
    BG_SUNK = "#16161e"  # sunken panes — sidebars, scroll regions
    LINE = "#414868"  # borders, dividers, scrollbars (faint)
    LINE_SOFT = "#2f334d"  # quiet inner borders / rails at rest

    # --- Foreground ---
    TEXT = "#c0caf5"  # primary foreground — lavender-white
    TEXT_DIM = "#a9b1d6"  # body copy / dimmed primary (textSoft)
    TEXT_MUTED = "#565f89"  # secondary & metadata text (muted)

    # --- Semantic accents ---
    ACCENT = "#7aa2f7"  # primary blue — headers, cursor, active states
    AGENT = "#9ece6a"  # green — assistant turn, ✓ icons, low-risk
    WARN = "#e0af68"  # amber — git tools, ⚠ approval, YOLO mode
    DANGER = "#f7768e"  # pink-red — shell tools, ✗ errors, high-risk
    INFO = "#7dcfff"  # cyan — fs tools, help menu, waiting state
    THOUGHT = "#bb9af7"  # purple — reasoning blocks, web tools


class Categories:
    """Tool-category rail colors.

    Each tool category owns a hue that colors its Rail (the left-edge pipe on a
    tool card). ``other`` is the neutral fallback.
    """

    FS = "#7dcfff"  # filesystem — cyan
    GIT = "#e0af68"  # git — amber
    SHELL = "#f7768e"  # shell — pink/danger
    WEB = "#bb9af7"  # web — purple
    SEARCH = "#7aa2f7"  # search — blue/accent
    AGENT = "#9ece6a"  # agent — green
    MCP = "#ff9e64"  # mcp — orange
    OTHER = "#a9b1d6"  # fallback — blue-gray

    _MAP = {
        "fs": FS,
        "filesystem": FS,
        "git": GIT,
        "shell": SHELL,
        "terminal": SHELL,
        "web": WEB,
        "search": SEARCH,
        "agent": AGENT,
        "subagent": AGENT,
        "mcp": MCP,
        "other": OTHER,
        "internal": OTHER,
    }

    @classmethod
    def color(cls, category: str | None) -> str:
        """Return the rail color for a tool category (falls back to ``OTHER``)."""
        return cls._MAP.get((category or "other").lower(), cls.OTHER)


class Glyphs:
    # Brand / turn markers
    BRAND = "◆"  # ◆ brand/agent mark — status bar model label
    USER = "❯"  # ❯ user turn / live composer prompt (caret)
    REASONING = "◆"  # ◆ reasoning
    ASSISTANT = "◈"  # ◈ assistant turn

    # Tool & status glyphs
    TOOL_OK = "✓"  # ✓ success
    TOOL_RUN = "⚙"  # ⚙ running / in-progress (cog)
    ERROR = "✗"  # ✗ error
    APPROVAL = "⚠"  # ⚠ caution / approval / medium+high risk
    DOT = "●"  # ● generic status dot (bullet)


class Styles:
    USER = f"bold {Tokens.INFO}"
    USER_GLYPH = f"bold {Tokens.INFO}"
    ASSISTANT = f"bold {Tokens.AGENT}"
    ASSISTANT_GLYPH = f"bold {Tokens.AGENT}"
    REASONING = f"italic {Tokens.THOUGHT}"
    REASONING_GLYPH = f"bold {Tokens.THOUGHT}"
    REASONING_LABEL = f"{Tokens.THOUGHT}"
    APPROVAL_GLYPH = f"bold {Tokens.WARN}"
    APPROVAL_LABEL = f"{Tokens.WARN}"
    TOOL_OK = Tokens.AGENT
    TOOL_NAME = Tokens.TEXT
    TOOL_ARGS = Tokens.TEXT_DIM
    TOOL_PREVIEW = Tokens.TEXT_MUTED
    TEXT_DIM = Tokens.TEXT_DIM
    TEXT_MUTED = Tokens.TEXT_MUTED
    TEXT = Tokens.TEXT
    SECTION = f"bold {Tokens.TEXT_DIM}"
    DANGER = f"bold {Tokens.DANGER}"
    GUTTER_LINE = Tokens.TEXT_MUTED
    GUTTER_ADD = Tokens.AGENT
    GUTTER_REMOVE = Tokens.DANGER
    GUTTER_CTX = Tokens.TEXT_DIM
    # Rich markup (Text.from_markup → RichLog) cannot parse rgba() colors —
    # they drop silently — so translucent backgrounds are pre-blended
    # against Tokens.BG into solid hex.
    DIFF_ADD_BG = "#272d2d"  # AGENT at 10% over BG
    DIFF_REMOVE_BG = "#302430"  # DANGER at 10% over BG
    # Intra-line word-diff emphasis: changed words on a paired −/+ line.
    DIFF_ADD_EMPH = f"bold {Tokens.AGENT} on #42513a"  # AGENT at 30% over BG
    DIFF_REMOVE_EMPH = f"bold {Tokens.DANGER} on #5c3645"  # DANGER at 30% over BG


__all__ = ["Tokens", "Categories", "Glyphs", "Styles"]
