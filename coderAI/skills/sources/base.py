"""Abstract base for skill source backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from coderAI.skills.skill_manager import Skill


class SkillSource(ABC):
    """A backend that can discover and search for skills.

    Concrete sources include:

    - ``LocalSkillSource`` — project-local ``.coderAI/skills/`` files
    - ``HasnaSkillSource`` — hosted ``@hasna/skills`` registry
    """

    @abstractmethod
    async def discover(self) -> List[Skill]:
        """Return every skill available from this source."""
        ...

    @abstractmethod
    async def search(self, query: str, top_n: int = 5) -> List[Tuple[Skill, float]]:
        """Search for skills relevant to *query*.

        Returns a list of ``(Skill, confidence)`` tuples sorted by
        descending confidence.
        """
        ...

    @abstractmethod
    async def get_skill(self, name: str) -> Optional[Skill]:
        """Retrieve a single skill by name from this source."""
        ...

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Human-readable label for this source (``"local"``, ``"hasna"``)."""
        ...
