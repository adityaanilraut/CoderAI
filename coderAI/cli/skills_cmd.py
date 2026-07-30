"""CLI subcommands for installing and managing skill workflows.

Skills are markdown workflows installed into:

* **project** — ``.coderAI/skills/<name>/SKILLS.md`` (requires workspace trust)
* **user** — ``~/.coderAI/skills/<name>/SKILLS.md`` (available in every project)

Sources mirror Claude Code / community installers: local paths, ``owner/repo``,
``owner/repo/path``, or GitHub URLs. Ecosystem ``SKILL.md`` files are accepted
and normalized to ``SKILLS.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.markup import escape

from coderAI.cli.utils import Display
from coderAI.skills.installer import (
    SKILL_SCOPES,
    SkillScope,
    InstallReport,
    discover_skill_candidates,
    install_from_source,
    list_installed_skills,
    parse_skill_source,
    remove_skill,
    skills_dir_for_scope,
)
from coderAI.skills.skill_manager import Skill, builtin_skills_root, discover_skills_in_directory


@click.group(invoke_without_command=True)
@click.pass_context
def skills(ctx: click.Context) -> None:
    """Manage skill workflows (install from GitHub or a local path)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@skills.command("install")
@click.argument("source")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Override the installed skill name (only when installing one skill).",
)
@click.option(
    "--path",
    "subpath",
    default=None,
    help="Subdirectory inside the source (e.g. skills/foo or plugins/bar/skills/baz).",
)
@click.option(
    "--ref",
    "-r",
    default=None,
    help="Git branch or tag (GitHub sources).",
)
@click.option(
    "--scope",
    "-s",
    type=click.Choice(list(SKILL_SCOPES)),
    default="project",
    show_default=True,
    help="Install into project (.coderAI/skills) or user (~/.coderAI/skills).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing skill with the same name.",
)
@click.option(
    "--only",
    multiple=True,
    help="When the source has many skills, install only these names (repeatable).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover skills and print destinations without copying.",
)
@click.option(
    "--list",
    "list_only",
    is_flag=True,
    help="List skills found in SOURCE without installing.",
)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Project root for --scope project (default: cwd).",
)
def skills_install(
    source: str,
    name: Optional[str],
    subpath: Optional[str],
    ref: Optional[str],
    scope: str,
    force: bool,
    only: tuple[str, ...],
    dry_run: bool,
    list_only: bool,
    project_root: Optional[Path],
) -> None:
    """Install a skill from a local path or GitHub.

    \b
    Examples:
      coderAI skills install ./my-skill
      coderAI skills install owner/repo
      coderAI skills install owner/repo/skills/foo --scope user
      coderAI skills install https://github.com/owner/repo --path skills/bar
      coderAI skills install owner/repo --list
    """
    display = Display()
    root = project_root or Path.cwd()
    scope_lit: SkillScope = "user" if scope == "user" else "project"

    try:
        if list_only:
            _list_source_skills(display, source, subpath=subpath, ref=ref)
            return

        report = install_from_source(
            source,
            scope=scope_lit,
            project_root=root,
            name=name,
            path=subpath,
            ref=ref,
            force=force,
            only=only or None,
            dry_run=dry_run,
        )
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
        display.print_error(str(exc))
        sys.exit(1)

    _print_install_report(display, report, dry_run=dry_run)
    if (
        not dry_run
        and scope_lit == "project"
        and any(r.status == "installed" for r in report.results)
    ):
        display.print_warning(
            "Project skill installs change the workspace trust fingerprint. "
            "Re-approve trust (/trust) and restart CoderAI so /skills can list them. "
            "Or install with --scope user to keep skills available without re-trust."
        )
    if any(r.status == "error" for r in report.results):
        sys.exit(1)
    if not report.installed and not dry_run and report.results:
        # All skipped (already installed) — not a hard failure.
        sys.exit(0)


def _list_source_skills(
    display: Display,
    source: str,
    *,
    subpath: Optional[str],
    ref: Optional[str],
) -> None:
    import tempfile
    from coderAI.skills.installer import fetch_source_tree

    parsed = parse_skill_source(source, ref=ref)
    path = subpath or parsed.subpath
    with tempfile.TemporaryDirectory(prefix="coderai-skills-list-") as tmp:
        tmp_path = Path(tmp)
        tree = fetch_source_tree(parsed, tmp_path / "repo" if parsed.kind == "github" else tmp_path)
        candidates = discover_skill_candidates(tree, subpath=path)
    if not candidates:
        display.print_warning("No skills found in source.")
        return
    display.print(f"Found {len(candidates)} skill(s) in {parsed.display or source}:")
    for c in candidates:
        desc = f" — {c.description}" if c.description else ""
        display.print(f"  • {c.name}{desc}")


def _print_install_report(display: Display, report: InstallReport, *, dry_run: bool) -> None:
    if dry_run:
        display.print(f"Dry run — {len(report.candidates)} candidate(s):")
        for r in report.results:
            display.print(f"  • {r.name} → {r.dest} [{r.scope}]")
        return

    for r in report.results:
        if r.status == "installed":
            display.print_success(f"Installed {r.name} → {r.dest}")
        elif r.status == "skipped":
            display.print_warning(f"Skipped {r.name}: {r.message}")
        else:
            display.print_error(f"Failed {r.name}: {r.message}")


@skills.command("list")
@click.option(
    "--scope",
    "-s",
    type=click.Choice(["all", *SKILL_SCOPES]),
    default="all",
    show_default=True,
    help="Which scope to list.",
)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Project root for project-scoped skills (default: cwd).",
)
def skills_list(scope: str, project_root: Optional[Path]) -> None:
    """List installed skills."""
    display = Display()
    root = project_root or Path.cwd()
    scope_filter: Optional[SkillScope]
    if scope == "all":
        scope_filter = None
    else:
        scope_filter = "user" if scope == "user" else "project"

    entries: list[tuple[str, Skill]] = list(
        list_installed_skills(scope=scope_filter, project_root=root)
    )
    if scope == "all":
        entries.extend(
            ("builtin", skill)
            for skill in discover_skills_in_directory(
                builtin_skills_root(),
                source="builtin",
            )
        )
    if not entries:
        display.print_warning(
            "No skills installed. Try `coderAI skills install owner/repo` "
            "or add `.coderAI/skills/<name>/SKILLS.md`."
        )
        return

    display.print(f"{len(entries)} skill(s):")
    for sc, skill in entries:
        desc = f" — {skill.description}" if skill.description else ""
        display.print(escape(f"  • [{sc}] {skill.name}{desc}"))


@skills.command("remove")
@click.argument("name")
@click.option(
    "--scope",
    "-s",
    type=click.Choice(list(SKILL_SCOPES)),
    default="project",
    show_default=True,
    help="Scope to remove from.",
)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Project root for --scope project (default: cwd).",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def skills_remove(
    name: str,
    scope: str,
    project_root: Optional[Path],
    yes: bool,
) -> None:
    """Remove an installed skill."""
    display = Display()
    root = project_root or Path.cwd()
    scope_lit: SkillScope = "user" if scope == "user" else "project"
    dest = skills_dir_for_scope(scope_lit, root) / name
    if not dest.is_dir():
        display.print_error(f"Skill {name!r} not found in {scope} scope ({dest}).")
        sys.exit(1)
    if not yes and not click.confirm(f"Remove {dest}?"):
        display.print("Cancelled.")
        return
    try:
        removed = remove_skill(name, scope=scope_lit, project_root=root)
    except (ValueError, FileNotFoundError) as exc:
        display.print_error(str(exc))
        sys.exit(1)
    display.print_success(f"Removed {removed}")
