"""In-memory prompt history with shell-style up/down recall.

Kept free of Textual imports so the navigation logic can be unit-tested in
isolation; :class:`coderAI.tui.screens.PromptArea` drives it from key events.
"""

from __future__ import annotations

from typing import List, Optional


class PromptHistory:
    """Tracks submitted prompts and supports up/down recall.

    The navigation model mirrors a shell line editor:

    - ``prev`` walks backward toward older entries. The first call stashes the
      live draft so ``next`` can restore it once the user walks back to the end.
    - ``next`` walks forward toward newer entries and finally back to the
      stashed draft, at which point navigation ends.
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._entries: List[str] = []
        self._max_entries = max_entries
        self._pos: Optional[int] = None
        self._stash: str = ""

    @property
    def entries(self) -> List[str]:
        return list(self._entries)

    @property
    def navigating(self) -> bool:
        return self._pos is not None

    def add(self, text: str) -> None:
        """Record a submitted prompt and end any active navigation.

        Blank prompts and immediate duplicates of the latest entry are ignored.
        """
        self.reset()
        if not text.strip():
            return
        if self._entries and self._entries[-1] == text:
            return
        self._entries.append(text)
        if len(self._entries) > self._max_entries:
            del self._entries[: -self._max_entries]

    def reset(self) -> None:
        """Stop navigating and forget the stashed draft."""
        self._pos = None
        self._stash = ""

    def prev(self, current: str) -> Optional[str]:
        """Return the previous (older) entry, or ``None`` if history is empty.

        ``current`` is the live draft, stashed on the first step back so it can
        be restored by walking forward with :meth:`next`.
        """
        if not self._entries:
            return None
        if self._pos is None:
            self._stash = current
            self._pos = len(self._entries)
        self._pos = max(0, self._pos - 1)
        return self._entries[self._pos]

    def next(self) -> Optional[str]:
        """Return the next (newer) entry, or the stashed draft at the end.

        Returns ``None`` when not currently navigating.
        """
        if self._pos is None:
            return None
        self._pos += 1
        if self._pos >= len(self._entries):
            draft = self._stash
            self.reset()
            return draft
        return self._entries[self._pos]
