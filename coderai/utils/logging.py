# Ported from coderai/core/common/debug_logger.py - kimi structure (kimi_cli/utils/logging.py).
""""""

from __future__ import annotations

import json
import pathlib
from coderai.log import (
    logger,
    enable_logging,
    redirect_stderr_to_logger,
    restore_stderr,
    open_original_stderr,
    StderrRedirector,
)

DEBUG_LOG_FILE = "debug.log"


def get_debug_log_path(project_root: str = ".") -> str:
    return str(pathlib.Path(project_root) / ".coderai" / DEBUG_LOG_FILE)


def log_openai_chat_completion_debug(entry: dict) -> None:
    try:
        path = pathlib.Path(get_debug_log_path(entry.get("projectRoot", ".")))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
# --- from coderai/core/common/error_logger.py ---
""""""


import pathlib
import re

LOG_DIR = pathlib.Path.home() / ".coderai" / "logs"
ERROR_LOG_PATH = LOG_DIR / "error.log"


def _mask_sensitive(text: str) -> str:
    text = re.sub(r"(Authorization:\s*Bearer\s+)[^\s\r\n]+", r"\1***MASKED***", text, flags=re.I)
    text = re.sub(
        r"((?:api[Kk]ey|api_key|secret)\s*[:=]\s*\"?)[^\",}\s]+",
        r"\1***MASKED***",
        text,
        flags=re.I,
    )
    return text


def log_api_error(error: Exception, context: dict | None = None) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        msg = _mask_sensitive(str(error)[:2000])
        line = f"[{error.__class__.__name__}] {msg}"
        if context:
            line += f" context={str(context)[:500]}"
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
