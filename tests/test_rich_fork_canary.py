"""Canary for the vendored rich-markdown fork (audit §3.9).

``coderai/utils/rich/markdown.py`` is forked from rich@4d6d631 and imports
private APIs (``rich._loop``, ``rich._stack``), so a rich upgrade can break it
silently. If ``test_pinned_rich_version`` fails after a rich bump, review the
fork's diff against the new upstream before updating the pin.
"""

from __future__ import annotations

import importlib.metadata

from rich.console import Console

from coderai.utils.rich.markdown import Markdown

PINNED_RICH_VERSION = "15.0.0"


def test_fork_renders_markdown() -> None:
    console = Console(width=80, force_terminal=False)
    with console.capture() as capture:
        console.print(Markdown("# Title\n\n- one\n- two\n"))
    out = capture.get()
    assert "Title" in out and "one" in out and "two" in out


def test_pinned_rich_version() -> None:
    installed = importlib.metadata.version("rich")
    assert installed == PINNED_RICH_VERSION, (
        f"rich is {installed}, fork was reviewed against {PINNED_RICH_VERSION}: "
        "diff coderai/utils/rich/markdown.py against the new upstream, then update the pin"
    )
