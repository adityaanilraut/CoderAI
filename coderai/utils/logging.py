""""""

from __future__ import annotations

import json
import pathlib
from coderai.log import (
    StderrRedirector as StderrRedirector,
    enable_logging as enable_logging,
    logger as logger,
    open_original_stderr as open_original_stderr,
    redact_secrets,
    redirect_stderr_to_logger as redirect_stderr_to_logger,
    restore_stderr as restore_stderr,
)

DEBUG_LOG_FILE = "debug.log"


def get_debug_log_path(project_root: str = ".") -> str:
    return str(pathlib.Path(project_root) / ".coderai" / DEBUG_LOG_FILE)


def log_openai_chat_completion_debug(entry: dict) -> None:
    try:
        path = pathlib.Path(get_debug_log_path(entry.get("projectRoot", ".")))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = redact_secrets(json.dumps(entry, ensure_ascii=False, default=str))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass


# --- from coderai/core/common/error_logger.py ---
""""""


LOG_DIR = pathlib.Path.home() / ".coderai" / "logs"
ERROR_LOG_PATH = LOG_DIR / "error.log"


def _mask_sensitive(text: str) -> str:
    """Back-compat alias for :func:`coderai.log.redact_secrets`."""
    return redact_secrets(text)


def log_api_error(error: Exception, context: dict | None = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"[{error.__class__.__name__}] {str(error)[:2000]}"
        if context:
            line += f" context={str(context)[:500]}"
        line = redact_secrets(line)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass
