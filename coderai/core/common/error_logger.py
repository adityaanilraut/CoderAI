"""Error logging — port of deepcode core/src/common/error-logger.ts."""

from __future__ import annotations

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
