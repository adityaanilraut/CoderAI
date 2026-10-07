"""Bounded, private attachment snapshots for per-workspace prompt recall."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

TOKEN = re.compile(r"\[attachments:([a-f0-9]{32})[^\]]*\]")


class SubmissionHistory:
    def __init__(self, root: str):
        from coderai.share import get_share_dir

        identity = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
        self.folder = get_share_dir() / "prompt-cache" / "submissions" / identity

    def save(self, text: str, attachments: list[dict]) -> str:
        if not attachments:
            return text
        self.folder.mkdir(parents=True, exist_ok=True)
        identity = uuid.uuid4().hex
        path = self.folder / f"{identity}.json"
        # tempfile's restrictive mode is retained by atomic replacement.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.folder, delete=False
        ) as handle:
            temporary = Path(handle.name)
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(attachments, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        files = sorted(
            self.folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        total = 0
        for index, item in enumerate(files):
            total += item.stat().st_size
            if index >= 30 or total > 200 * 1024 * 1024:
                item.unlink(missing_ok=True)
        names = ", ".join(str(a.get("name", "image")).replace("]", "") for a in attachments)
        return f"{TOKEN.sub('', text).strip()} [attachments:{identity} {names}]"

    def resolve(self, text: str) -> tuple[str, list[dict]]:
        attachments = []
        for match in TOKEN.finditer(text):
            path = self.folder / f"{match[1]}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "These history attachments are unavailable. Attach the images again."
                ) from exc
            if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                raise ValueError("Invalid history attachment snapshot. Attach the images again.")
            attachments.extend(data)
        return TOKEN.sub("", text).strip(), attachments
