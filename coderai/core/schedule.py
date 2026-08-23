"""Schedule Subsystem — durable session-scoped timers and recurring reminders."""

from __future__ import annotations

import datetime
import json
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

MIN_EVERY_INTERVAL_SECONDS = 300  # 5 minutes


@dataclass
class ScheduleRecord:
    id: str
    prompt: str
    kind: str  # "after" | "at" | "every"
    scheduled_at: str  # ISO 8601 UTC
    created_at: str  # ISO 8601 UTC
    state: str = "scheduled"  # "scheduled" | "overdue" | "dispatched" | "deleted"
    delivery_mode: str = "session-local"
    after_seconds: int | None = None
    every_seconds: int | None = None
    target_timestamp: float = 0.0
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "prompt": self.prompt,
            "kind": self.kind,
            "scheduledAt": self.scheduled_at,
            "createdAt": self.created_at,
            "state": self.state,
            "deliveryMode": self.delivery_mode,
        }
        if self.session_id:
            d["sessionId"] = self.session_id
        if self.after_seconds is not None:
            d["afterSeconds"] = self.after_seconds
        if self.every_seconds is not None:
            d["everySeconds"] = self.every_seconds
        return d


class ScheduleManager:
    """Manages scheduled timers and recurring reminders."""

    def __init__(self, storage_path: str | None = None) -> None:
        self.storage_path = storage_path
        self._schedules: dict[str, ScheduleRecord] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._load()

    def create(
        self,
        prompt: str,
        after_seconds: int | None = None,
        at: str | dict[str, Any] | None = None,
        every_seconds: int | None = None,
        session_id: str | None = None,
    ) -> ScheduleRecord:
        """Create a new schedule record."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Parameter `prompt` must be a non-empty string.")

        # Exactly one selector must be provided
        selectors = [s for s in (after_seconds, at, every_seconds) if s is not None]
        if len(selectors) != 1:
            raise ValueError(
                "Exactly one of `after_seconds`, `at`, or `every_seconds` must be provided."
            )

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_ts = now_utc.timestamp()

        if after_seconds is not None:
            try:
                after_s = int(after_seconds)
                if after_s <= 0:
                    raise ValueError("`after_seconds` must be a positive integer.")
            except (ValueError, TypeError):
                raise ValueError("`after_seconds` must be a positive integer.")

            target_ts = now_ts + after_s
            target_dt = datetime.datetime.fromtimestamp(target_ts, tz=datetime.timezone.utc)
            rec = ScheduleRecord(
                id=self._generate_id(),
                prompt=prompt,
                kind="after",
                scheduled_at=target_dt.isoformat().replace("+00:00", "Z"),
                created_at=now_utc.isoformat().replace("+00:00", "Z"),
                after_seconds=after_s,
                target_timestamp=target_ts,
                session_id=session_id,
            )

        elif every_seconds is not None:
            try:
                every_s = int(every_seconds)
                if every_s < MIN_EVERY_INTERVAL_SECONDS:
                    raise ValueError(
                        f"`every_seconds` must be at least {MIN_EVERY_INTERVAL_SECONDS} seconds (5 minutes)."
                    )
            except (ValueError, TypeError):
                raise ValueError("`every_seconds` must be a valid integer.")

            target_ts = now_ts + every_s
            target_dt = datetime.datetime.fromtimestamp(target_ts, tz=datetime.timezone.utc)
            rec = ScheduleRecord(
                id=self._generate_id(),
                prompt=prompt,
                kind="every",
                scheduled_at=target_dt.isoformat().replace("+00:00", "Z"),
                created_at=now_utc.isoformat().replace("+00:00", "Z"),
                every_seconds=every_s,
                target_timestamp=target_ts,
                session_id=session_id,
            )

        else:  # at selector
            target_dt = self._parse_at(at, now_utc)
            target_ts = target_dt.timestamp()
            if target_ts <= now_ts:
                raise ValueError("Scheduled `at` time must be in the future.")

            rec = ScheduleRecord(
                id=self._generate_id(),
                prompt=prompt,
                kind="at",
                scheduled_at=target_dt.isoformat().replace("+00:00", "Z"),
                created_at=now_utc.isoformat().replace("+00:00", "Z"),
                target_timestamp=target_ts,
                session_id=session_id,
            )

        with self._lock:
            self._schedules[rec.id] = rec
            self._save()

        return rec

    def list_schedules(self, session_id: str | None = None) -> list[ScheduleRecord]:
        """Return active and overdue schedules in creation order."""
        now_ts = time.time()
        with self._lock:
            active = []
            for rec in self._schedules.values():
                if session_id and rec.session_id and rec.session_id != session_id:
                    continue
                if rec.state in ("scheduled", "overdue"):
                    if rec.target_timestamp <= now_ts and rec.state == "scheduled":
                        rec.state = "overdue"
                    active.append(rec)
            return sorted(active, key=lambda x: x.created_at)

    def delete(self, schedule_id: str) -> bool:
        """Delete / cancel a schedule by id."""
        with self._lock:
            if schedule_id in self._schedules:
                rec = self._schedules[schedule_id]
                rec.state = "deleted"
                del self._schedules[schedule_id]
                self._save()
                return True
            return False

    def check_due(self, session_id: str | None = None) -> list[ScheduleRecord]:
        """Return all schedules that have reached or passed their deadline."""
        now_ts = time.time()
        due = []
        with self._lock:
            for rec in list(self._schedules.values()):
                if session_id and rec.session_id and rec.session_id != session_id:
                    continue
                if rec.state in ("scheduled", "overdue") and rec.target_timestamp <= now_ts:
                    due.append(rec)
                    if rec.kind == "every" and rec.every_seconds:
                        # Advance recurring target
                        rec.target_timestamp = now_ts + rec.every_seconds
                        next_dt = datetime.datetime.fromtimestamp(
                            rec.target_timestamp, tz=datetime.timezone.utc
                        )
                        rec.scheduled_at = next_dt.isoformat().replace("+00:00", "Z")
                        rec.state = "scheduled"
                    else:
                        rec.state = "dispatched"
            self._save()
        return due

    def _generate_id(self) -> str:
        with self._lock:
            sid = f"sched_{self._next_id}"
            self._next_id += 1
            return sid

    def _parse_at(self, at_input: Any, now_utc: datetime.datetime) -> datetime.datetime:
        if isinstance(at_input, str):
            # Parse RFC3339 / ISO 8601 string
            clean_str = at_input.strip()
            if clean_str.endswith("Z"):
                clean_str = clean_str[:-1] + "+00:00"
            try:
                dt = datetime.datetime.fromisoformat(clean_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt.astimezone(datetime.timezone.utc)
            except Exception as exc:
                raise ValueError(f"Invalid RFC3339 timestamp string for `at`: {at_input} ({exc})")

        elif isinstance(at_input, dict):
            date_str = at_input.get("date")
            time_str = at_input.get("time")
            tz_str = at_input.get("time_zone", "UTC")
            if not date_str or not time_str:
                raise ValueError(
                    "`at` object must contain `date` (YYYY-MM-DD) and `time` (HH:MM:SS)."
                )

            tz: datetime.tzinfo
            try:
                tz = ZoneInfo(tz_str)
            except Exception:
                tz = datetime.timezone.utc

            try:
                dt_local = datetime.datetime.fromisoformat(f"{date_str}T{time_str}")
                dt_local = dt_local.replace(tzinfo=tz)
                return dt_local.astimezone(datetime.timezone.utc)
            except Exception as exc:
                raise ValueError(f"Invalid date/time components for `at`: {exc}")

        raise ValueError("`at` must be an ISO 8601 string or `{date, time, time_zone}` object.")

    def _save(self) -> None:
        if not self.storage_path:
            return
        try:
            p = pathlib.Path(self.storage_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nextId": self._next_id,
                "schedules": [s.to_dict() for s in self._schedules.values()],
            }
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load(self) -> None:
        if not self.storage_path:
            return
        p = pathlib.Path(self.storage_path)
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._next_id = data.get("nextId", 1)
            for item in data.get("schedules", []):
                rec = ScheduleRecord(
                    id=item["id"],
                    prompt=item["prompt"],
                    kind=item["kind"],
                    scheduled_at=item["scheduledAt"],
                    created_at=item["createdAt"],
                    state=item.get("state", "scheduled"),
                    delivery_mode=item.get("deliveryMode", "session-local"),
                    after_seconds=item.get("afterSeconds"),
                    every_seconds=item.get("everySeconds"),
                    session_id=item.get("sessionId"),
                )
                self._schedules[rec.id] = rec
        except Exception:
            pass


_default_schedule_manager: ScheduleManager | None = None


def get_schedule_manager(storage_path: str | None = None) -> ScheduleManager:
    global _default_schedule_manager
    if _default_schedule_manager is None:
        _default_schedule_manager = ScheduleManager(storage_path=storage_path)
    return _default_schedule_manager
