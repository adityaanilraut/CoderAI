"""Tests for coderAI skills install / list / remove."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coderAI.cli.skills_cmd import skills
from coderAI.skills.installer import (
    discover_skill_candidates,
    install_from_source,
    parse_skill_source,
    remove_skill,
    validate_skill_name,
)
from coderAI.skills.skill_manager import (
    SKILLS_FILE_NAME,
    discover_local_skills,
    load_skill_by_name,
)


@pytest.fixture
def runner():
    return CliRunner()


def _write_skill(dir_path: Path, name: str, *, filename: str = SKILLS_FILE_NAME) -> Path:
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True)
    (skill_dir / filename).write_text(
        f"---\nname: {name}\ndescription: Test skill {name}\n---\n\n# {name}\n\nDo the thing.\n",
        encoding="utf-8",
    )
    return skill_dir


class TestParseSource:
    def test_local_path(self, tmp_path):
        skill = _write_skill(tmp_path, "local-one")
        parsed = parse_skill_source(str(skill))
        assert parsed.kind == "local"
        assert parsed.local_path == skill.resolve()

    def test_owner_repo(self):
        parsed = parse_skill_source("acme/awesome-skills")
        assert parsed.kind == "github"
        assert parsed.owner == "acme"
        assert parsed.repo == "awesome-skills"
        assert parsed.subpath is None

    def test_owner_repo_path(self):
        parsed = parse_skill_source("acme/awesome-skills/skills/foo")
        assert parsed.kind == "github"
        assert parsed.subpath == "skills/foo"

    def test_github_https_with_tree(self):
        parsed = parse_skill_source("https://github.com/acme/awesome-skills/tree/main/skills/foo")
        assert parsed.kind == "github"
        assert parsed.owner == "acme"
        assert parsed.repo == "awesome-skills"
        assert parsed.ref == "main"
        assert parsed.subpath == "skills/foo"

    def test_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unrecognized"):
            parse_skill_source("not a source!!!")


class TestValidateName:
    def test_ok(self):
        assert validate_skill_name("security-audit") == "security-audit"

    def test_rejects_path(self):
        with pytest.raises(ValueError):
            validate_skill_name("../evil")


class TestGithubArchiveRefs:
    """An explicit ``--ref`` must never silently resolve to the default branch."""

    def _record_attempts(self, monkeypatch, succeed_on=None):
        from coderAI.skills import installer

        attempted: list[str] = []

        def fake_extract(url, dest):
            attempted.append(url)
            if succeed_on is not None and succeed_on in url:
                return
            raise RuntimeError("404 Not Found")

        monkeypatch.setattr(installer, "_extract_zipball", fake_extract)
        return attempted

    def test_explicit_ref_does_not_fall_back_to_default_branches(self, monkeypatch, tmp_path):
        from coderAI.skills.installer import _download_github_archive, parse_skill_source

        attempted = self._record_attempts(monkeypatch)
        parsed = parse_skill_source("https://github.com/acme/pack/tree/v1.2.0/skills")

        with pytest.raises(RuntimeError, match="Failed to fetch ref 'v1.2.0'"):
            _download_github_archive(parsed, tmp_path)

        assert attempted == [
            "https://github.com/acme/pack/archive/refs/heads/v1.2.0.zip",
            "https://github.com/acme/pack/archive/refs/tags/v1.2.0.zip",
        ]

    def test_explicit_ref_resolves_as_a_tag(self, monkeypatch, tmp_path):
        from coderAI.skills.installer import _download_github_archive, parse_skill_source

        attempted = self._record_attempts(monkeypatch, succeed_on="tags/v1.2.0")
        parsed = parse_skill_source("https://github.com/acme/pack/tree/v1.2.0/skills")

        assert _download_github_archive(parsed, tmp_path) == tmp_path
        assert attempted[-1].endswith("refs/tags/v1.2.0.zip")

    def test_no_ref_still_probes_main_then_master(self, monkeypatch, tmp_path):
        from coderAI.skills.installer import _download_github_archive, parse_skill_source

        attempted = self._record_attempts(monkeypatch, succeed_on="heads/master")
        parsed = parse_skill_source("acme/pack")

        assert _download_github_archive(parsed, tmp_path) == tmp_path
        assert attempted == [
            "https://github.com/acme/pack/archive/refs/heads/main.zip",
            "https://github.com/acme/pack/archive/refs/heads/master.zip",
        ]


class TestDiscoverCandidates:
    def test_single_skill_dir(self, tmp_path):
        skill = _write_skill(tmp_path, "solo", filename="SKILL.md")
        found = discover_skill_candidates(skill)
        assert len(found) == 1
        assert found[0].name == "solo"

    def test_skills_subdir(self, tmp_path):
        root = tmp_path / "repo"
        skills = root / "skills"
        _write_skill(skills, "a")
        _write_skill(skills, "b", filename="SKILL.md")
        found = discover_skill_candidates(root)
        assert {c.name for c in found} == {"a", "b"}

    def test_plugins_layout(self, tmp_path):
        root = tmp_path / "repo"
        plugin_skills = root / "plugins" / "pack" / "skills"
        _write_skill(plugin_skills, "from-plugin", filename="SKILL.md")
        found = discover_skill_candidates(root)
        assert [c.name for c in found] == ["from-plugin"]


class TestInstallLocal:
    def test_install_project_normalizes_skill_md(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "src"
        _write_skill(src, "legacy", filename="SKILL.md")

        report = install_from_source(str(src / "legacy"), scope="project", project_root=tmp_path)
        assert len(report.installed) == 1
        dest = tmp_path / ".coderAI" / "skills" / "legacy"
        assert (dest / SKILLS_FILE_NAME).is_file()
        assert not (dest / "SKILL.md").exists()

        skills = discover_local_skills(str(tmp_path), include_user=False)
        assert [s.name for s in skills] == ["legacy"]

    def test_install_user_scope(self, tmp_path, monkeypatch):
        monkeypatch.setattr("coderAI.system.config.config_manager.config_dir", tmp_path / "home")
        src = tmp_path / "src"
        _write_skill(src, "global-skill")

        report = install_from_source(str(src / "global-skill"), scope="user", project_root=tmp_path)
        assert report.installed[0].status == "installed"
        dest = tmp_path / "home" / "skills" / "global-skill"
        assert (dest / SKILLS_FILE_NAME).is_file()

        skill = load_skill_by_name(
            "global-skill",
            str(tmp_path),
            include_project=False,
            include_user=True,
        )
        assert skill is not None
        assert skill.source == "user"

    def test_skip_without_force(self, tmp_path):
        src = tmp_path / "src"
        _write_skill(src, "dup")
        install_from_source(str(src / "dup"), scope="project", project_root=tmp_path)
        report = install_from_source(str(src / "dup"), scope="project", project_root=tmp_path)
        assert report.results[0].status == "skipped"

    def test_force_overwrite(self, tmp_path):
        src = tmp_path / "src"
        _write_skill(src, "dup")
        install_from_source(str(src / "dup"), scope="project", project_root=tmp_path)
        (src / "dup" / SKILLS_FILE_NAME).write_text(
            "---\nname: dup\ndescription: Updated\n---\n\n# Updated\n",
            encoding="utf-8",
        )
        report = install_from_source(
            str(src / "dup"), scope="project", project_root=tmp_path, force=True
        )
        assert report.installed[0].status == "installed"
        skill = load_skill_by_name("dup", str(tmp_path), include_user=False)
        assert skill is not None
        assert skill.description == "Updated"

    def test_remove(self, tmp_path):
        src = tmp_path / "src"
        _write_skill(src, "gone")
        install_from_source(str(src / "gone"), scope="project", project_root=tmp_path)
        remove_skill("gone", scope="project", project_root=tmp_path)
        assert load_skill_by_name("gone", str(tmp_path), include_user=False) is None


class TestSkillsCli:
    def test_install_list_remove(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("coderAI.system.config.config_manager.config_dir", tmp_path / "home")
        src = tmp_path / "bundle"
        _write_skill(src / "skills", "alpha")
        _write_skill(src / "skills", "beta", filename="SKILL.md")

        result = runner.invoke(
            skills,
            ["install", str(src), "--project-root", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output

        listed = runner.invoke(
            skills, ["list", "--scope", "project", "--project-root", str(tmp_path)]
        )
        assert listed.exit_code == 0, listed.output
        assert "alpha" in listed.output
        assert "beta" in listed.output

        removed = runner.invoke(
            skills,
            ["remove", "alpha", "--project-root", str(tmp_path), "--yes"],
        )
        assert removed.exit_code == 0, removed.output
        assert load_skill_by_name("alpha", str(tmp_path), include_user=False) is None
        assert load_skill_by_name("beta", str(tmp_path), include_user=False) is not None

    def test_list_source_only(self, runner, tmp_path):
        src = tmp_path / "bundle"
        _write_skill(src / "skills", "gamma")
        result = runner.invoke(skills, ["install", str(src), "--list"])
        assert result.exit_code == 0, result.output
        assert "gamma" in result.output
        assert not (tmp_path / ".coderAI" / "skills" / "gamma").exists()
