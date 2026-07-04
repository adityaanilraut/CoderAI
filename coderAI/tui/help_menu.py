"""Slash command catalog for /help.

Derived from the slash registry so the palette can never drift from the
commands that actually dispatch — add a command via ``_register`` in
``slash.py`` and it appears here automatically.
"""

from coderAI.tui.slash import COMMAND_SPECS

HELP_MENU_ENTRIES = [("/" + spec.names[0], spec.desc) for spec in COMMAND_SPECS]
