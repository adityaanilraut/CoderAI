"""Installed built-in capability discovery and scope precedence."""

from __future__ import annotations

from pathlib import Path

from coderAI.assets.manifest import (
    BUILTIN_PERSONAS,
    BUILTIN_RULES,
    BUILTIN_SKILLS,
    verify_builtin_assets,
)
from coderAI.core.personas import (
    get_available_persona_descriptors,
    load_agent_persona,
)
from coderAI.skills.skill_manager import discover_local_skills, load_skill_by_name
from coderAI.system.config import config_manager


def _persona(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: planner\ndescription: test planner\ntools: []\n---\n" + marker,
        encoding="utf-8",
    )


def _skill(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nname: security-audit\ndescription: test audit\n---\n" + marker,
        encoding="utf-8",
    )


def test_builtin_asset_manifest_is_complete() -> None:
    assert verify_builtin_assets() == {
        "personas": list(BUILTIN_PERSONAS),
        "skills": list(BUILTIN_SKILLS),
        "rules": list(BUILTIN_RULES),
    }


def test_empty_project_discovers_packaged_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "empty-user-config")

    personas = get_available_persona_descriptors(str(tmp_path))
    skills = discover_local_skills(str(tmp_path))

    assert [item.name for item in personas] == list(BUILTIN_PERSONAS)
    assert {item.scope for item in personas} == {"builtin"}
    assert [item.name for item in skills] == list(BUILTIN_SKILLS)
    assert {item.source for item in skills} == {"builtin"}


def test_persona_precedence_is_project_then_user_then_builtin(tmp_path, monkeypatch) -> None:
    user_config = tmp_path / "user-config"
    monkeypatch.setattr(config_manager, "config_dir", user_config)
    _persona(user_config / "agents" / "planner.md", "USER PERSONA")
    _persona(tmp_path / "project" / ".coderAI" / "agents" / "planner.md", "PROJECT PERSONA")

    project = load_agent_persona("planner", str(tmp_path / "project"))
    user = load_agent_persona("planner", str(tmp_path / "project"), include_project=False)
    builtin = load_agent_persona(
        "planner",
        str(tmp_path / "project"),
        include_project=False,
        include_user=False,
    )

    assert project is not None and project.source == "project"
    assert project.instructions == "PROJECT PERSONA"
    assert user is not None and user.source == "user"
    assert user.instructions == "USER PERSONA"
    assert builtin is not None and builtin.source == "builtin"
    assert "specific, incremental, and testable" in builtin.instructions


def test_skill_precedence_is_project_then_user_then_builtin(tmp_path, monkeypatch) -> None:
    user_config = tmp_path / "user-config"
    monkeypatch.setattr(config_manager, "config_dir", user_config)
    _skill(user_config / "skills" / "security-audit" / "SKILLS.md", "USER SKILL")
    _skill(
        tmp_path / "project" / ".coderAI" / "skills" / "security-audit" / "SKILLS.md",
        "PROJECT SKILL",
    )

    project = load_skill_by_name("security-audit", str(tmp_path / "project"))
    user = load_skill_by_name("security-audit", str(tmp_path / "project"), include_project=False)
    builtin = load_skill_by_name(
        "security-audit",
        str(tmp_path / "project"),
        include_project=False,
        include_user=False,
    )

    assert project is not None and (project.source, project.instructions) == (
        "project",
        "PROJECT SKILL",
    )
    assert user is not None and (user.source, user.instructions) == ("user", "USER SKILL")
    assert builtin is not None and builtin.source == "builtin"
    assert "security audit workflow" in builtin.instructions.lower()


def test_persona_and_skill_symlink_escapes_are_not_discovered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "empty-user-config")
    outside_persona = tmp_path / "outside.md"
    _persona(outside_persona, "OUTSIDE PERSONA")
    persona_link = tmp_path / "project" / ".coderAI" / "agents" / "escaped.md"
    persona_link.parent.mkdir(parents=True)
    persona_link.symlink_to(outside_persona)

    outside_skill = tmp_path / "outside-skill"
    _skill(outside_skill / "SKILLS.md", "OUTSIDE SKILL")
    skill_link = tmp_path / "project" / ".coderAI" / "skills" / "escaped"
    skill_link.parent.mkdir(parents=True)
    skill_link.symlink_to(outside_skill, target_is_directory=True)

    personas = get_available_persona_descriptors(
        str(tmp_path / "project"), include_user=False, include_builtin=False
    )
    skills = discover_local_skills(
        str(tmp_path / "project"), include_user=False, include_builtin=False
    )

    assert personas == []
    assert skills == []


def test_symlinked_project_scope_roots_are_not_discovered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "empty-user-config")
    outside = tmp_path / "outside"
    _persona(outside / "agents" / "external.md", "OUTSIDE PERSONA")
    _skill(outside / "skills" / "external" / "SKILLS.md", "OUTSIDE SKILL")
    project_config = tmp_path / "project" / ".coderAI"
    project_config.mkdir(parents=True)
    (project_config / "agents").symlink_to(outside / "agents", target_is_directory=True)
    (project_config / "skills").symlink_to(outside / "skills", target_is_directory=True)

    personas = get_available_persona_descriptors(
        str(tmp_path / "project"), include_user=False, include_builtin=False
    )
    skills = discover_local_skills(
        str(tmp_path / "project"), include_user=False, include_builtin=False
    )

    assert personas == []
    assert skills == []


def test_persona_path_syntax_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_manager, "config_dir", tmp_path / "empty-user-config")

    assert load_agent_persona("../planner", str(tmp_path)) is None
    assert load_agent_persona("folder/planner", str(tmp_path)) is None
