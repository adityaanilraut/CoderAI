"""ASCII art and gradient banner styling for CoderAI CLI."""

from __future__ import annotations

try:
    from rich.style import Style
    from rich.text import Text

    _RICH = True
except ImportError:  # pragma: no cover
    Style = None  # type: ignore[assignment,misc]
    Text = None  # type: ignore[assignment,misc]
    _RICH = False

import shutil

CODERAI_ASCII_LOGO = [
    " ██████╗  ██████╗ ██████╗ ███████╗██████╗  █████╗ ██╗",
    "██╔════╝ ██╔═══██╗██╔══██╗██╔════╝██╔══██╗██╔══██╗██║",
    "██║      ██║   ██║██║  ██║█████╗  ██████╔╝███████║██║",
    "██║      ██║   ██║██║  ██║██╔══╝  ██╔══██╗██╔══██║██║",
    "╚██████╗ ╚██████╔╝██████╔╝███████╗██║  ██║██║  ██║██║",
    " ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝",
]

# Color palette: Gradient from Neon Cyan (#00E5FF) to Deep Magenta (#E040FB)
GRADIENT_COLORS = [
    (0, 229, 255),  # #00E5FF - Neon Cyan
    (0, 195, 255),  # #00C3FF - Bright Sky
    (79, 138, 255),  # #4F8AFF - Vibrant Blue
    (147, 83, 255),  # #9353FF - Purple
    (198, 64, 255),  # #C640FF - Orchid
    (224, 64, 251),  # #E040FB - Neon Magenta
]


def _interpolate_color(
    c1: tuple[int, int, int], c2: tuple[int, int, int], factor: float
) -> tuple[int, int, int]:
    return (
        int(c1[0] + (c2[0] - c1[0]) * factor),
        int(c1[1] + (c2[1] - c1[1]) * factor),
        int(c1[2] + (c2[2] - c1[2]) * factor),
    )


def get_compact_gradient_badge() -> Text | str:
    """Return a sleek single-line gradient badge for compact or narrow terminal displays."""
    title = "CoderAI"
    if not _RICH or Text is None or Style is None:
        return title

    text = Text()
    for idx, char in enumerate(title):
        factor = idx / max(1, len(title) - 1)
        segment_pos = factor * (len(GRADIENT_COLORS) - 1)
        seg_idx = min(int(segment_pos), len(GRADIENT_COLORS) - 2)
        seg_factor = segment_pos - seg_idx
        r, g, b = _interpolate_color(
            GRADIENT_COLORS[seg_idx], GRADIENT_COLORS[seg_idx + 1], seg_factor
        )
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        text.append(char, style=f"bold {hex_color}")
    return text


def get_gradient_ascii_logo(force_full: bool = False) -> Text | str:
    """Return the stylized CoderAI ASCII logo with smooth gradient interpolation or compact banner if narrow."""
    # Check terminal width
    columns, _ = shutil.get_terminal_size(fallback=(80, 24))
    if not force_full and columns < 58:
        return get_compact_gradient_badge()

    if not _RICH or Text is None or Style is None:
        return "\n".join(CODERAI_ASCII_LOGO)

    text = Text()
    num_lines = len(CODERAI_ASCII_LOGO)

    for line_idx, line in enumerate(CODERAI_ASCII_LOGO):
        line_len = len(line)
        if line_len == 0:
            text.append("\n")
            continue

        for col_idx, char in enumerate(line):
            # 2D diagonal gradient blending line and col position
            factor = (line_idx / max(1, num_lines - 1) * 0.4) + (
                col_idx / max(1, line_len - 1) * 0.6
            )
            factor = min(1.0, max(0.0, factor))

            # Select gradient segment
            segment_pos = factor * (len(GRADIENT_COLORS) - 1)
            seg_idx = min(int(segment_pos), len(GRADIENT_COLORS) - 2)
            seg_factor = segment_pos - seg_idx

            r, g, b = _interpolate_color(
                GRADIENT_COLORS[seg_idx], GRADIENT_COLORS[seg_idx + 1], seg_factor
            )
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            text.append(char, style=f"bold {hex_color}")
        text.append("\n")

    return text
