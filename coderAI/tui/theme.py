"""Design tokens for the CoderAI Textual UI.

Hex equivalents kept in one place so widgets, modal screens, and Rich Text
styles all agree on the palette and glyph language.
"""

from __future__ import annotations


class Tokens:
    BG = "#1d1b16"
    BG_RAISED = "#26241e"
    BG_SUNK = "#181610"
    LINE = "#3a362e"
    LINE_SOFT = "#312e27"
    TEXT = "#ece8df"
    TEXT_DIM = "#aea99b"
    TEXT_MUTED = "#807a6a"

    AGENT = "#7fd49a"
    WARN = "#d6bd6f"
    DANGER = "#d57b66"
    INFO = "#82b8d6"
    THOUGHT = "#c498d0"


class Glyphs:
    USER = "\u276f"          # user turn / composer prompt
    REASONING = "\u25c6"     # reasoning
    ASSISTANT = "\u25c8"     # assistant turn
    TOOL_OK = "\u2713"       # tool ok
    TOOL_RUN = "\u25f4"      # tool running
    APPROVAL = "\u25b2"      # approval / warning
    ERROR = "\u2717"         # error
    DOT = "\u25cf"
    PARENT = "\u21b3"
    TREE_END = "\u2514"      # tree last child
    TREE_MID = "\u251c"      # tree middle child


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
    TOOL_RUN = Tokens.THOUGHT
    TOOL_ERR = Tokens.DANGER
    TOOL_NAME = Tokens.TEXT
    TOOL_ARGS = Tokens.TEXT_DIM
    TOOL_PREVIEW = Tokens.TEXT_MUTED
    TEXT_DIM = Tokens.TEXT_DIM
    TEXT_MUTED = Tokens.TEXT_MUTED
    TEXT = Tokens.TEXT
    SECTION = f"bold {Tokens.TEXT_DIM}"
    DANGER = f"bold {Tokens.DANGER}"
    WARN = f"bold {Tokens.WARN}"
    GUTTER_LINE = Tokens.TEXT_MUTED
    GUTTER_ADD = "oklch(0.86 0.12 145)"
    GUTTER_REMOVE = "oklch(0.78 0.14 25)"
    GUTTER_CTX = Tokens.TEXT_DIM
    DIFF_ADD_BG = "rgba(120,255,170,0.06)"
    DIFF_REMOVE_BG = "rgba(120,120,255,0.06)"


__all__ = ["Tokens", "Glyphs", "Styles"]
