"""Debug logging — port of deepcode core/src/common/debug-logger.ts."""

from __future__ import annotations

import json
import pathlib

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
