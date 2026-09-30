"""Centralized terminal color theme definitions.

Provides dark/light switching for diff background colors, syntax theme,
task browser, MCP status, and prompt styling. Pure CLI, no browser layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.styles import Style as PTKStyle
from rich.style import Style as RichStyle

ThemeName = Literal["dark", "light"]


# ---------------------------------------------------------------------------
# Diff colors (used by utils/rich/diff_render.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffColors:
    add_bg: RichStyle
    del_bg: RichStyle
    add_hl: RichStyle
    del_hl: RichStyle


_DIFF_DARK = DiffColors(
    add_bg=RichStyle(bgcolor="#12261e"),
    del_bg=RichStyle(bgcolor="#2d1214"),
    add_hl=RichStyle(bgcolor="#1a4a2e"),
    del_hl=RichStyle(bgcolor="#5c1a1d"),
)

_DIFF_LIGHT = DiffColors(
    add_bg=RichStyle(bgcolor="#dafbe1"),
    del_bg=RichStyle(bgcolor="#ffebe9"),
    add_hl=RichStyle(bgcolor="#aff5b4"),
    del_hl=RichStyle(bgcolor="#ffc1c0"),
)


# ---------------------------------------------------------------------------
# Task browser colors (used by ui/shell/task_browser.py)
# ---------------------------------------------------------------------------


def _task_browser_style_dark() -> PTKStyle:
    return PTKStyle.from_dict(
        {
            "header": "bg:#1f2937 #e5e7eb",
            "header.title": "bg:#1f2937 #67e8f9 bold",
            "header.meta": "bg:#1f2937 #9ca3af",
            "status.running": "bg:#1f2937 #86efac bold",
            "status.success": "bg:#1f2937 #86efac",
            "status.warning": "bg:#1f2937 #fbbf24",
            "status.error": "bg:#1f2937 #fca5a5",
            "status.info": "bg:#1f2937 #93c5fd",
            "task-list": "bg:#111827 #d1d5db",
            "task-list.checked": "bg:#164e63 #ecfeff bold",
            "frame.border": "#155e75",
            "frame.label": "bg:#0f172a #67e8f9 bold",
            "footer": "bg:#0f172a #cbd5e1",
            "footer.key": "bg:#0f172a #67e8f9 bold",
            "footer.text": "bg:#0f172a #cbd5e1",
            "footer.warning": "bg:#7f1d1d #fecaca bold",
            "footer.meta": "bg:#0f172a #94a3b8",
        }
    )


def _task_browser_style_light() -> PTKStyle:
    return PTKStyle.from_dict(
        {
            "header": "bg:#e5e7eb #1f2937",
            "header.title": "bg:#e5e7eb #0e7490 bold",
            "header.meta": "bg:#e5e7eb #6b7280",
            "status.running": "bg:#e5e7eb #166534 bold",
            "status.success": "bg:#e5e7eb #166534",
            "status.warning": "bg:#e5e7eb #92400e",
            "status.error": "bg:#e5e7eb #991b1b",
            "status.info": "bg:#e5e7eb #1e40af",
            "task-list": "bg:#f9fafb #374151",
            "task-list.checked": "bg:#cffafe #164e63 bold",
            "frame.border": "#0e7490",
            "frame.label": "bg:#f1f5f9 #0e7490 bold",
            "footer": "bg:#f1f5f9 #475569",
            "footer.key": "bg:#f1f5f9 #0e7490 bold",
            "footer.text": "bg:#f1f5f9 #475569",
            "footer.warning": "bg:#fee2e2 #991b1b bold",
            "footer.meta": "bg:#f1f5f9 #64748b",
        }
    )


# ---------------------------------------------------------------------------
# Prompt / completion menu colors (used by ui/shell/prompt.py)
# ---------------------------------------------------------------------------


_PROMPT_STYLE_DARK = {
    "bottom-toolbar": "noreverse",
    "running-prompt-placeholder": "fg:#7c8594 italic",
    "running-prompt-separator": "fg:#4a5568",
    "slash-completion-menu": "",
    "slash-completion-menu.separator": "fg:#4a5568",
    "slash-completion-menu.marker": "fg:#4a5568",
    "slash-completion-menu.marker.current": "fg:#4f9fff",
    "slash-completion-menu.command": "fg:#a6adba",
    "slash-completion-menu.meta": "fg:#7c8594",
    "slash-completion-menu.command.current": "fg:#6fb7ff bold",
    "slash-completion-menu.meta.current": "fg:#56a4ff",
}

_PROMPT_STYLE_LIGHT = {
    "bottom-toolbar": "noreverse",
    "running-prompt-placeholder": "fg:#6b7280 italic",
    "running-prompt-separator": "fg:#d1d5db",
    "slash-completion-menu": "",
    "slash-completion-menu.separator": "fg:#d1d5db",
    "slash-completion-menu.marker": "fg:#9ca3af",
    "slash-completion-menu.marker.current": "fg:#2563eb",
    "slash-completion-menu.command": "fg:#4b5563",
    "slash-completion-menu.meta": "fg:#6b7280",
    "slash-completion-menu.command.current": "fg:#1d4ed8 bold",
    "slash-completion-menu.meta.current": "fg:#2563eb",
}


# ---------------------------------------------------------------------------
# Bottom toolbar fragment colors (used by ui/shell/prompt.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolbarColors:
    separator: str
    yolo_label: str
    afk_label: str
    plan_label: str
    plan_prompt: str
    cwd: str
    bg_tasks: str
    tip: str
    # Shared bar background plus the fragments that used to be hardcoded in
    # the prompt session. Every fragment is painted onto `background` so
    # light/dark switches don't leave holes in the status bar.
    background: str
    text: str
    model: str
    tokens: str
    git: str
    role: str
    turns: str
    mcp: str


_TOOLBAR_DARK = ToolbarColors(
    separator="fg:#4d4d4d",
    yolo_label="bold fg:#ffff00",
    afk_label="bold fg:#ff8800",
    plan_label="bold fg:#00aaff",
    plan_prompt="fg:#00aaff",
    cwd="fg:#666666",
    bg_tasks="fg:#888888",
    tip="fg:#555555",
    background="#1e1e2e",
    text="#cdd6f4",
    model="bold #89dceb",
    tokens="#a6e3a1",
    git="#cba6f7",
    role="bold #cba6f7",
    turns="#89b4fa",
    mcp="#94e2d5",
)

_TOOLBAR_LIGHT = ToolbarColors(
    separator="fg:#d1d5db",
    yolo_label="bold fg:#b45309",
    afk_label="bold fg:#c2410c",
    plan_label="bold fg:#2563eb",
    plan_prompt="fg:#2563eb",
    cwd="fg:#6b7280",
    bg_tasks="fg:#4b5563",
    tip="fg:#9ca3af",
    background="#f1f5f9",
    text="#1f2937",
    model="bold #0e7490",
    tokens="#166534",
    git="#7c3aed",
    role="bold #7c3aed",
    turns="#1d4ed8",
    mcp="#0f766e",
)


# ---------------------------------------------------------------------------
# MCP status prompt colors (used by ui/shell/mcp_status.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MCPPromptColors:
    text: str
    detail: str
    connected: str
    connecting: str
    pending: str
    failed: str


_MCP_PROMPT_DARK = MCPPromptColors(
    text="fg:#d4d4d4",
    detail="fg:#7c8594",
    connected="fg:#56d364",
    connecting="fg:#56a4ff",
    pending="fg:#f2cc60",
    failed="fg:#ff7b72",
)

_MCP_PROMPT_LIGHT = MCPPromptColors(
    text="fg:#374151",
    detail="fg:#6b7280",
    connected="fg:#166534",
    connecting="fg:#1d4ed8",
    pending="fg:#92400e",
    failed="fg:#dc2626",
)


# ---------------------------------------------------------------------------
# Public API — resolve by theme name
# ---------------------------------------------------------------------------

_active_theme: ThemeName = "dark"


def set_active_theme(theme: ThemeName) -> None:
    global _active_theme
    _active_theme = theme


def get_active_theme() -> ThemeName:
    return _active_theme


def get_diff_colors() -> DiffColors:
    return _DIFF_LIGHT if _active_theme == "light" else _DIFF_DARK


def get_task_browser_style() -> PTKStyle:
    return _task_browser_style_light() if _active_theme == "light" else _task_browser_style_dark()


def get_prompt_style() -> PTKStyle:
    d = _PROMPT_STYLE_LIGHT if _active_theme == "light" else _PROMPT_STYLE_DARK
    return PTKStyle.from_dict(d)


def get_toolbar_colors() -> ToolbarColors:
    return _TOOLBAR_LIGHT if _active_theme == "light" else _TOOLBAR_DARK


def _paint_toolbar(background: str, fragment: str) -> str:
    """Attach the shared toolbar background to a foreground fragment."""
    return f"bg:{background} {fragment}"


_COMPLETION_DARK = {
    "prompt": "bold",
    "prompt.plan": "bold #f9e2af",
    "completion-menu": "bg:#181825 #cdd6f4",
    "completion-menu.completion": "bg:#181825 #cdd6f4",
    "completion-menu.completion.current": "bg:#313244 #89b4fa bold",
    "completion-menu.meta": "bg:#181825 #6c7086",
    "completion-menu.meta.completion.current": "bg:#313244 #a6adc8",
    "completion-menu.multi-column-meta": "bg:#181825 #6c7086",
    "scrollbar.background": "bg:#181825",
    "scrollbar.button": "bg:#45475a",
    "fuzzymatch.inside": "nobold nounderline",
    "fuzzymatch.outside": "nobold nounderline",
}

_COMPLETION_LIGHT = {
    "prompt": "bold",
    "prompt.plan": "bold #b45309",
    "completion-menu": "bg:#f8fafc #1f2937",
    "completion-menu.completion": "bg:#f8fafc #1f2937",
    "completion-menu.completion.current": "bg:#e0f2fe #0e7490 bold",
    "completion-menu.meta": "bg:#f8fafc #6b7280",
    "completion-menu.meta.completion.current": "bg:#e0f2fe #334155",
    "completion-menu.multi-column-meta": "bg:#f8fafc #6b7280",
    "scrollbar.background": "bg:#f8fafc",
    "scrollbar.button": "bg:#cbd5e1",
    "fuzzymatch.inside": "nobold nounderline",
    "fuzzymatch.outside": "nobold nounderline",
}


def get_prompt_session_styles() -> dict[str, str]:
    """Full prompt_toolkit style map for the input session.

    Toolbar fragments all share one background so the status bar stays a
    single strip in both themes. Completion-menu colors follow the same theme.
    """
    colors = get_toolbar_colors()
    bg = colors.background
    toolbar = {
        "toolbar": _paint_toolbar(bg, colors.text),
        "toolbar.model": _paint_toolbar(bg, colors.model),
        "toolbar.tokens": _paint_toolbar(bg, colors.tokens),
        "toolbar.git": _paint_toolbar(bg, colors.git),
        "toolbar.role": _paint_toolbar(bg, colors.role),
        "toolbar.plan": _paint_toolbar(bg, colors.plan_label),
        "toolbar.yolo": _paint_toolbar(bg, colors.yolo_label),
        "toolbar.afk": _paint_toolbar(bg, colors.afk_label),
        "toolbar.turns": _paint_toolbar(bg, colors.turns),
        "toolbar.mcp": _paint_toolbar(bg, colors.mcp),
        "toolbar.cwd": _paint_toolbar(bg, colors.cwd),
        "toolbar.sep": _paint_toolbar(bg, colors.separator),
        "toolbar.tip": _paint_toolbar(bg, colors.tip),
        "toolbar.extra": _paint_toolbar(bg, colors.text),
    }
    menus = _COMPLETION_LIGHT if _active_theme == "light" else _COMPLETION_DARK
    return {**toolbar, **menus}


def get_mcp_prompt_colors() -> MCPPromptColors:
    return _MCP_PROMPT_LIGHT if _active_theme == "light" else _MCP_PROMPT_DARK


# ponytail: truecolor probe is coarse (env sniff), per-console detection if needed later
def supports_truecolor() -> bool:
    """Return True if terminal likely supports 24-bit truecolor; graceful fallback to 256."""
    import os
    import sys

    if os.getenv("NO_COLOR") is not None:
        return False
    colorterm = (os.getenv("COLORTERM") or "").lower()
    if colorterm in ("truecolor", "24bit"):
        return True
    term = (os.getenv("TERM") or "").lower()
    if "truecolor" in term or "24bit" in term:
        return True
    # xterm-256color alone is 256, not truecolor — treat as fallback
    if not sys.stdout.isatty():
        return False
    return False


def get_color_system() -> str | None:
    """Rich color_system hint: 'truecolor' or '256' or None for autodetect."""
    import os

    if os.getenv("NO_COLOR") is not None:
        return None
    return "truecolor" if supports_truecolor() else "256"
