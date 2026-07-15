"""Central logging configuration for CLI and TUI modes.

CLI/headless mode logs to stderr (the historical behavior). TUI mode logs to
a rotating file under ``~/.coderAI/logs/`` because any stderr write while
Textual owns the terminal corrupts the display. Set ``CODERAI_LOG_STDERR=1``
to force a stderr handler in TUI mode (debugging escape hatch).
"""

import logging
import logging.handlers
import os
import stat
from pathlib import Path
from typing import Optional

from coderAI.system.redaction import RedactingFilter, RedactingFormatter

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DIR = Path.home() / ".coderAI" / "logs"
LOG_FILE = LOG_DIR / "coderai.log"

# Marker attribute so we only ever replace handlers we installed, never
# handlers added by tests or embedding applications.
_MANAGED_ATTR = "_coderai_managed"


def _resolve_level(level: Optional[int]) -> int:
    if level is not None:
        return level
    try:
        from coderAI.system.config import config_manager

        cfg_level = getattr(config_manager.load(), "log_level", "WARNING")
        return getattr(logging, str(cfg_level).upper(), logging.WARNING)
    except Exception:
        return logging.WARNING


def _make_file_handler() -> logging.Handler:
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    try:
        os.chmod(LOG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return handler


def setup_logging(level: Optional[int] = None, *, tui_mode: bool = False) -> None:
    """(Re)configure root logging for the current run mode.

    Removes only handlers previously installed by this function, so callers
    can switch between CLI and TUI modes without clobbering foreign handlers.
    """
    root = logging.getLogger()
    resolved = _resolve_level(level)

    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            root.removeHandler(handler)
            handler.close()

    handlers: list = []
    if tui_mode:
        handlers.append(_make_file_handler())
        if os.environ.get("CODERAI_LOG_STDERR") == "1":
            handlers.append(logging.StreamHandler())
    else:
        handlers.append(logging.StreamHandler())

    formatter = RedactingFormatter(LOG_FORMAT)
    redacting = RedactingFilter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redacting)
        setattr(handler, _MANAGED_ATTR, True)
        root.addHandler(handler)

    root.setLevel(resolved)
