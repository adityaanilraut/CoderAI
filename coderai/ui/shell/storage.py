"""Private, atomic shell recovery and navigation preferences per workspace."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import time
from pathlib import Path
from typing import Any

from coderai.ui.shell.submission import PromptSubmission
from coderai.utils.storage import read_bytes, write_bytes, storage_lock


class ShellStorage:
    def __init__(self, root: str) -> None:
        from coderai.share import get_share_dir

        identity = sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
        self.folder = get_share_dir().resolve() / "shell" / identity

    def _read(self, name: str) -> dict:
        try:
            value = json.loads(read_bytes(self.folder / name))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("Invalid saved shell state")
        return value

    def _write(self, name: str, value: dict) -> None:
        write_bytes(self.folder / name, json.dumps(value, ensure_ascii=False).encode())

    @staticmethod
    def view_name(sid: str | None) -> str:
        return "view-" + sha256((sid or "new").encode()).hexdigest()[:32] + ".json"

    def save_view(
        self, view: Any, *, draft: str | None = None, revision: int | None = None
    ) -> None:
        value = {
            "revision": revision or time.time_ns(),
            "session_id": view.session_id,
            "plan_mode": view.plan_mode,
            "draft": view.draft if draft is None else draft,
            "cursor": view.cursor,
            "attachments": view.attachments,
            "pending_skills": view.pending_skills,
            "queue": [asdict(item) for item in view.queue],
            "last_failed": asdict(view.last_failed) if view.last_failed else None,
        }
        with storage_lock(self.folder / "recovery.json"):
            name = self.view_name(view.session_id)
            if self._read(name).get("revision", 0) > value["revision"]:
                return
            self._write(name, value)
            if self._read("last-view.json").get("revision", 0) <= value["revision"]:
                self._write(
                    "last-view.json", {"session_id": view.session_id, "revision": value["revision"]}
                )

    def load_view(self, sid: str | None) -> dict:
        value = self._read(self.view_name(sid))
        if not value or value.get("deleted"):
            return {}
        value.pop("revision", None)
        if value.get("session_id") != sid or not isinstance(value.get("draft"), str):
            raise ValueError("Invalid recovered draft")
        for key in ("attachments", "pending_skills", "queue"):
            if not isinstance(value.get(key), list):
                raise ValueError("Invalid recovered submission")
        if (value.get("cursor") is not None and not isinstance(value["cursor"], int)) or not all(
            isinstance(a, dict) for a in value["attachments"]
        ):
            raise ValueError("Invalid recovered attachments or cursor")
        value["queue"] = [PromptSubmission(**item) for item in value["queue"]]
        if any(
            item.action not in ("send", "queue", "steer", "shell", "command")
            for item in value["queue"]
        ):
            raise ValueError("Invalid recovered queue action")
        if value.get("last_failed"):
            value["last_failed"] = PromptSubmission(**value["last_failed"])
        # Recovery must never execute queued work without an explicit /queue run.
        value["queue_paused"] = bool(value["queue"])
        return value

    def last_session(self) -> str | None:
        return self._read("last-view.json").get("session_id")

    def delete_view(self, sid: str | None) -> None:
        with storage_lock(self.folder / "recovery.json"):
            self._write(self.view_name(sid), {"deleted": True, "revision": time.time_ns()})

    def library(self) -> dict:
        return self._read("library.json")

    def update_library(self, section: str, key: str, value: Any) -> None:
        path = self.folder / "library.json"
        with storage_lock(path):
            data = self.library()
            data.setdefault(section, {})[key] = value
            self._write("library.json", data)

    def remember_model(self, model: str) -> None:
        self.update_library("recent_models", model, time.time())
