"""Additive user display preferences; no session/wire format changes."""

from dataclasses import asdict, dataclass
import os
from typing import Literal


def accessible_enabled() -> bool:
    """Static fallback menus honor the same additive user preference."""
    if os.getenv("CODERAI_ACCESSIBLE") == "1":
        return True
    from coderai.config import read_settings

    values = (read_settings() or {}).get("display") or {}
    return isinstance(values, dict) and bool(values.get("accessible"))


@dataclass
class DisplayPreferences:
    detail: Literal["compact", "verbose"] = "compact"
    accessible: bool = False
    theme: Literal["dark", "light"] = "dark"

    @classmethod
    def load(cls, root: str) -> "DisplayPreferences":
        from coderai.config import resolve_current_settings

        values = resolve_current_settings(root).get("display", {})
        if not isinstance(values, dict):
            values = {}
        return cls(
            "verbose" if values.get("detail") == "verbose" else "compact",
            bool(values.get("accessible", False) or os.getenv("CODERAI_ACCESSIBLE") == "1"),
            "light" if values.get("theme") == "light" else "dark",
        )

    def save(self, root: str) -> None:
        from coderai.config import save_setting_key

        save_setting_key("display", asdict(self), scope="user", project_root=root)
