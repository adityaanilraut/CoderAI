"""Dynamic skill loading for CoderAI agents.

Provides proactive LLM-based skill matching that automatically discovers
and injects relevant skill instructions before task execution.

Usage::

    from coderAI.skills import SkillManager, LocalSkillSource

    manager = SkillManager(
        sources=[LocalSkillSource(project_root)],
        threshold=0.7,
        top_n=3,
        provider=agent.provider,
    )
    skills = await manager.get_top_skills("analyze CSV data")
    for skill in skills:
        print(skill.instructions)
"""

from coderAI.skills.skill_manager import (
    Skill,
    SkillRegistry,
    SkillManager,
    discover_local_skills,
    load_skill_by_name,
)
from coderAI.skills.sources.base import SkillSource
from coderAI.skills.sources.local_source import LocalSkillSource

__all__ = [
    "Skill",
    "SkillRegistry",
    "SkillManager",
    "SkillSource",
    "LocalSkillSource",
    "discover_local_skills",
    "load_skill_by_name",
]
