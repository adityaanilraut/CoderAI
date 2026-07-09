"""Abstract base for skill source backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from coderAI.skills.skill_manager import Skill


class SkillSource(ABC):
    """A backend that can discover skills.

    The built-in source is ``LocalSkillSource`` (project-local
    ``.coderAI/skills/<name>/SKILLS.md``).
    """

    @abstractmethod
    async def discover(self) -> List[Skill]:
        """Return every skill available from this source."""
        ...

    @abstractmethod
    async def get_skill(self, name: str) -> Optional[Skill]:
        """Retrieve a single skill by name from this source."""
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable label for this source (e.g. ``"local"``)."""
        ...
