"""Design tokens for the CoderAI Textual UI.

Hex equivalents of the OKLCH values in the redesign bundle. Kept in one
place so widgets, modal screens, and Rich Text styles all agree on the
palette and glyph language.
"""

from __future__ import annotations


class Tokens:
    # warm-neutral dark base — 60° hue keeps it off clinical-blue
    BG = "#1d1b16"  # oklch(0.18 0.01 60)
    BG_RAISED = "#26241e"  # oklch(0.22 0.012 60)
    BG_SUNK = "#181610"  # oklch(0.15 0.01 60)
    LINE = "#3a362e"  # oklch(0.30 0.012 60)
    LINE_SOFT = "#312e27"  # oklch(0.26 0.012 60)

    TEXT = "#ece8df"  # oklch(0.94 0.005 80)
    TEXT_DIM = "#aea99b"  # oklch(0.72 0.008 80)
    TEXT_MUTED = "#807a6a"  # oklch(0.55 0.008 80)

    # signals — shared chroma/lightness, only hue varies
    AGENT = "#7fd49a"  # green   — agent / success / primary action
    WARN = "#d6bd6f"  # amber   — approval / risk / hot toggles
    DANGER = "#d57b66"  # red     — error / destructive
    INFO = "#82b8d6"  # blue    — user / info
    THOUGHT = "#c498d0"  # violet  — reasoning / sub-agents
    PALETTE_SELECTED_BG = "#14b3d1b8"  # oklch(0.82 0.15 145 / 0.08)


class Glyphs:
    USER = "❯"  # user turn / composer prompt
    REASONING = "◆"  # reasoning
    ASSISTANT = "◈"  # assistant turn
    TOOL_OK = "✓"  # tool ok
    TOOL_RUN = "◴"  # tool running
    APPROVAL = "▲"  # approval / warning
    ERROR = "✗"  # error
    PINNED = "⚑"  # pinned context
    DOT = "●"
    PARENT = "↳"
    TREE_CONT = "│"  # tree continuation
    TREE_END = "└"  # tree last child
    TREE_MID = "├"  # tree middle child


# Rich style strings keyed off the tokens above. Centralised so callers
# don't sprinkle raw colour codes across modules.
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
    LABEL = f"{Tokens.TEXT_DIM}"
    SECTION = f"bold {Tokens.TEXT_DIM}"
    DANGER = f"bold {Tokens.DANGER}"
    WARN = f"bold {Tokens.WARN}"
    AGENT_BADGE = f"bold {Tokens.AGENT}"
    THOUGHT_BADGE = f"bold {Tokens.THOUGHT}"
    INFO_BADGE = f"bold {Tokens.INFO}"
    CHIP_LABEL = f"{Tokens.TEXT_MUTED}"
    CHIP_VALUE = f"{Tokens.TEXT}"
    TOOL_BG = Tokens.BG_SUNK
    TOOL_BORDER = Tokens.LINE_SOFT
    GUTTER_LINE = Tokens.TEXT_MUTED
    GUTTER_ADD = "oklch(0.86 0.12 145)"
    GUTTER_REMOVE = "oklch(0.78 0.14 25)"
    GUTTER_CTX = Tokens.TEXT_DIM
    DIFF_ADD_BG = "rgba(120,255,170,0.06)"
    DIFF_REMOVE_BG = "rgba(255,120,120,0.06)"


__all__ = ["Tokens", "Glyphs", "Styles"]
