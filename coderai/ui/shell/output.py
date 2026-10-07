"""Bounded file previews and retained tool output with stable identifiers."""

from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
from typing import Any


from coderai.utils.file_tail import tail_file as tail_file


def message_output(message: Any, mgr: Any) -> str:
    """Readable full tool output, with a session-owned spill when present."""
    content = message.content or ""
    if message.role != "tool":
        return (message.thinking + "\n" if message.thinking else "") + content
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return content
    if not isinstance(payload, dict):
        return content
    output = payload.get("output")
    metadata = payload.get("metadata") or {}
    spill = metadata.get("spill") if isinstance(metadata, dict) else None
    notice = ""
    if isinstance(spill, dict) and spill.get("locator"):
        from coderai.spill import resolve_spill_root, session_dir
        from hashlib import sha256

        root = resolve_spill_root({"session_manager": mgr, "project_root": mgr.project_root})
        path = Path(str(spill["locator"]))
        if root is not None and path.resolve().is_relative_to(
            session_dir(root, message.session_id).resolve()
        ):
            try:
                data = path.read_bytes()
                if spill.get("sha256") and sha256(data).hexdigest() != spill["sha256"]:
                    raise ValueError("Retained output has changed since execution.")
                output = data.decode("utf-8", errors="replace")
            except (OSError, ValueError) as exc:
                notice = f"\nComplete retained output is unavailable: {exc}\n"
        else:
            notice = "\nRetained output belongs to another session or has an invalid path.\n"
    if not isinstance(output, str):
        return json.dumps(payload, ensure_ascii=False, indent=2)
    details = {key: value for key, value in payload.items() if key != "output"}
    return (
        output
        + notice
        + "\n\nResult metadata:\n"
        + json.dumps(details, ensure_ascii=False, indent=2)
    )


class OutputStore:
    def __init__(self, limit: int = 100) -> None:
        self.items: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.limit = limit

    def retain(self, identifier: str, title: str, text: str) -> None:
        self.items[identifier] = (title, text)
        self.items.move_to_end(identifier)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)

    def clear(self) -> None:
        self.items.clear()
