"""Design tokens for the CoderAI Textual UI.

Palette: Tokyo Night-inspired surfaces with a modern **Aurora** accent ramp.
Colors are *earned*: the UI defaults to muted/faint tones and promotes to a
semantic color only when something genuinely needs attention.

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
    COMPOSER_BG = "#242528"  # neutral graphite — message composer
    COMPOSER_LINE = "#45474b"  # neutral composer outline
    LINE = "#414868"  # borders, dividers, scrollbars (faint)
    LINE_SOFT = "#2f334d"  # quiet inner borders / rails at rest

    # --- Foreground ---
    TEXT = "#c0caf5"  # primary foreground — lavender-white
    TEXT_DIM = "#a9b1d6"  # body copy / dimmed primary (textSoft)
    TEXT_MUTED = "#565f89"  # secondary & metadata text (muted)

    # --- Semantic accents (Aurora) ---
    # Bright 400-level hues stay legible on every dark surface while giving
    # active, success, caution, and reasoning states distinct silhouettes.
    ACCENT = "#818cf8"  # electric indigo — headers, cursor, active states
    AGENT = "#34d399"  # emerald — assistant turn, ✓ icons, low-risk
    WARN = "#fbbf24"  # amber — git tools, ⚠ approval, YOLO mode
    DANGER = "#fb7185"  # rose — shell tools, ✗ errors, high-risk
    INFO = "#22d3ee"  # cyan — fs tools, help menu, waiting state
    THOUGHT = "#c084fc"  # orchid — reasoning blocks, web tools


class Categories:
    """Tool-category rail colors.

    Each tool category owns a hue that colors its Rail (the left-edge pipe on a
    tool card). ``other`` is the neutral fallback.
    """

    FS = Tokens.INFO  # filesystem — cyan
    GIT = Tokens.WARN  # git — amber
    SHELL = Tokens.DANGER  # shell — pink/danger
    WEB = Tokens.THOUGHT  # web — purple
    SEARCH = Tokens.ACCENT  # search — blue/accent
    AGENT = Tokens.AGENT  # agent — green
    MCP = "#fb923c"  # mcp — orange (no matching semantic token)
    OTHER = Tokens.TEXT_DIM  # fallback — blue-gray

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
    REASONING = "◇"  # ◇ reasoning (hollow, distinct from brand ◆)
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
    # Terminal "dim" attribute — de-emphasize a whole line (incl. nested color
    # tags, which a color swap couldn't reach). Centralized so it isn't a raw
    # `[dim]` scattered through renderers.
    DE_EMPHASIS = "dim"
    SECTION = f"bold {Tokens.TEXT_DIM}"
    DANGER = f"bold {Tokens.DANGER}"
    GUTTER_LINE = Tokens.TEXT_MUTED
    GUTTER_ADD = Tokens.AGENT
    GUTTER_REMOVE = Tokens.DANGER
    GUTTER_CTX = Tokens.TEXT_DIM
    # Rich markup (Text.from_markup → RichLog) cannot parse rgba() colors —
    # they drop silently — so translucent backgrounds are pre-blended
    # against Tokens.BG into solid hex.
    DIFF_ADD_BG = "#1d2d32"  # AGENT at 10% over BG
    DIFF_REMOVE_BG = "#31242c"  # DANGER at 10% over BG
    # Intra-line word-diff emphasis: changed words on a paired −/+ line.
    DIFF_ADD_EMPH = f"bold {Tokens.AGENT} on #225249"  # AGENT at 30% over BG
    DIFF_REMOVE_EMPH = f"bold {Tokens.DANGER} on #5e3341"  # DANGER at 30% over BG


__all__ = ["Tokens", "Categories", "Glyphs", "Styles"]
