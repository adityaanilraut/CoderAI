"""Install skills from local paths or GitHub into project/user skill dirs.

Mirrors the Claude Code / community ``skills install`` workflow:

* ``coderAI skills install owner/repo``
* ``coderAI skills install owner/repo/path/to/skill``
* ``coderAI skills install https://github.com/owner/repo``
* ``coderAI skills install ./local-skill-dir``

Installed content is normalized to ``.coderAI/skills/<name>/SKILLS.md``
(accepting ecosystem ``SKILL.md`` as input).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional
from collections.abc import Iterable, Sequence
from urllib.request import Request, urlopen

from coderAI.skills.skill_manager import (
    SKILLS_DIR_NAME,
    SKILLS_FILE_NAME,
    Skill,
    discover_skills_in_directory,
    load_skill_from_path,
    skill_markdown_path,
)
from coderAI.system.config import config_manager
from coderAI.system.fsperms import OWNER_RWX, restrict_path

logger = logging.getLogger(__name__)

SkillScope = Literal["project", "user"]
SKILL_SCOPES: tuple[SkillScope, ...] = ("project", "user")

_GITHUB_HTTPS = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<subpath>.*))?)?/?$"
)
_GITHUB_SSH = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_OWNER_REPO_PATH = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:/(?P<subpath>.+))?$"
)
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
_FETCH_TIMEOUT_SEC = 60


@dataclass(frozen=True)
class ParsedSource:
    """Normalized install source."""

    kind: Literal["local", "github"]
    local_path: Optional[Path] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    ref: Optional[str] = None
    subpath: Optional[str] = None
    display: str = ""


@dataclass
class SkillCandidate:
    """A skill directory discovered inside a source tree."""

    name: str
    source_dir: Path
    description: str = ""
    skill_file: Optional[Path] = None


@dataclass
class InstallResult:
    """Outcome of one skill install attempt."""

    name: str
    dest: Path
    scope: SkillScope
    status: Literal["installed", "skipped", "error"]
    message: str = ""
    source: str = ""


@dataclass
class InstallReport:
    results: list[InstallResult] = field(default_factory=list)
    candidates: list[SkillCandidate] = field(default_factory=list)

    @property
    def installed(self) -> list[InstallResult]:
        return [r for r in self.results if r.status == "installed"]


def user_skills_dir() -> Path:
    """Return ``~/.coderAI/skills`` (created on demand by callers)."""
    return Path(config_manager.config_dir) / SKILLS_DIR_NAME


def project_skills_dir(project_root: str | Path = ".") -> Path:
    """Return ``<project>/.coderAI/skills`` (not created)."""
    return Path(project_root).resolve() / ".coderAI" / SKILLS_DIR_NAME


def skills_dir_for_scope(scope: SkillScope, project_root: str | Path = ".") -> Path:
    if scope == "user":
        return user_skills_dir()
    return project_skills_dir(project_root)


def parse_skill_source(source: str, *, ref: Optional[str] = None) -> ParsedSource:
    """Parse a local path, ``owner/repo[/path]``, or GitHub URL into a source."""
    raw = (source or "").strip()
    if not raw:
        raise ValueError("Skill source is required.")

    local = Path(raw).expanduser()
    if local.exists():
        return ParsedSource(
            kind="local",
            local_path=local.resolve(),
            display=str(local.resolve()),
            subpath=None,
            ref=ref,
        )

    m = _GITHUB_HTTPS.match(raw.rstrip("/"))
    if m:
        repo = m.group("repo")
        return ParsedSource(
            kind="github",
            owner=m.group("owner"),
            repo=repo,
            ref=ref or m.group("ref"),
            subpath=_clean_subpath(m.group("subpath")),
            display=f"github.com/{m.group('owner')}/{repo}",
        )

    m = _GITHUB_SSH.match(raw)
    if m:
        return ParsedSource(
            kind="github",
            owner=m.group("owner"),
            repo=m.group("repo"),
            ref=ref,
            display=f"github.com/{m.group('owner')}/{m.group('repo')}",
        )

    m = _OWNER_REPO_PATH.match(raw)
    if m and not raw.startswith(".") and "/" in raw:
        return ParsedSource(
            kind="github",
            owner=m.group("owner"),
            repo=m.group("repo"),
            ref=ref,
            subpath=_clean_subpath(m.group("subpath")),
            display=f"github.com/{m.group('owner')}/{m.group('repo')}",
        )

    raise ValueError(
        f"Unrecognized skill source {source!r}. Use a local path, "
        "owner/repo, owner/repo/path, or a GitHub URL."
    )


def _clean_subpath(subpath: Optional[str]) -> Optional[str]:
    if not subpath:
        return None
    cleaned = subpath.strip().strip("/")
    if not cleaned or ".." in Path(cleaned).parts:
        return None
    return cleaned


def validate_skill_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not _SAFE_SKILL_NAME.match(cleaned):
        raise ValueError(
            f"Invalid skill name {name!r}. Use letters, digits, '.', '_', or '-' (max 64 chars)."
        )
    return cleaned


def discover_skill_candidates(
    root: Path,
    *,
    subpath: Optional[str] = None,
) -> list[SkillCandidate]:
    """Find skill folders under ``root`` (optionally rooted at ``subpath``)."""
    base = root
    if subpath:
        base = (root / subpath).resolve()
        # is_relative_to, not a string prefix: the latter also accepts a sibling
        # directory whose name merely starts with the root's (``<root>-evil``).
        if not base.is_relative_to(root.resolve()):
            raise ValueError(f"Unsafe subpath: {subpath!r}")
        if not base.exists():
            raise FileNotFoundError(f"Path not found in source: {subpath}")

    candidates: list[SkillCandidate] = []
    seen: set[str] = set()

    def _add(skill_dir: Path) -> None:
        skill_file = skill_markdown_path(skill_dir)
        if skill_file is None:
            return
        skill = load_skill_from_path(skill_file, source="install")
        name = skill.name if skill else skill_dir.name
        try:
            name = validate_skill_name(name)
        except ValueError:
            logger.warning("Skipping skill with unsafe name at %s", skill_dir)
            return
        if name in seen:
            return
        seen.add(name)
        candidates.append(
            SkillCandidate(
                name=name,
                source_dir=skill_dir,
                description=(skill.description if skill else ""),
                skill_file=skill_file,
            )
        )

    # Single skill at the target root.
    if skill_markdown_path(base) is not None:
        _add(base)
        return candidates

    skills_subdir = base / SKILLS_DIR_NAME
    search_roots: list[Path] = []
    if skills_subdir.is_dir():
        search_roots.append(skills_subdir)
    search_roots.append(base)

    for search in search_roots:
        try:
            children = sorted(search.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                _add(child)

    # Plugin-style: plugins/<name>/skills/<skill>/
    plugins = base / "plugins"
    if plugins.is_dir():
        for plugin in sorted(plugins.iterdir()):
            plugin_skills = plugin / SKILLS_DIR_NAME
            if not plugin_skills.is_dir():
                continue
            for child in sorted(plugin_skills.iterdir()):
                if child.is_dir():
                    _add(child)

    return candidates


def fetch_source_tree(parsed: ParsedSource, dest: Path) -> Path:
    """Materialize a source tree into ``dest`` and return the tree root."""
    if parsed.kind == "local":
        assert parsed.local_path is not None
        return parsed.local_path

    assert parsed.owner and parsed.repo
    dest.mkdir(parents=True, exist_ok=True)
    if _try_git_clone(parsed, dest):
        return dest
    return _download_github_archive(parsed, dest)


def _try_git_clone(parsed: ParsedSource, dest: Path) -> bool:
    url = f"https://github.com/{parsed.owner}/{parsed.repo}.git"
    cmd = ["git", "clone", "--depth", "1"]
    if parsed.ref:
        cmd.extend(["--branch", parsed.ref])
    cmd.extend([url, str(dest)])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("git clone unavailable: %s", exc)
        return False
    if result.returncode != 0:
        logger.debug("git clone failed: %s", (result.stderr or result.stdout).strip())
        return False
    return True


def _download_github_archive(parsed: ParsedSource, dest: Path) -> Path:
    """Fallback: download GitHub zipball when git is unavailable.

    An explicit ``ref`` is a requirement, not a hint — it is tried as a branch
    then as a tag, and a miss is an error. Falling through to ``main``/``master``
    would quietly install a different version of the skill than the user asked
    for. Only when no ref was given do we probe the usual default branches.
    """
    base = f"https://github.com/{parsed.owner}/{parsed.repo}/archive/refs"
    if parsed.ref:
        urls = [f"{base}/heads/{parsed.ref}.zip", f"{base}/tags/{parsed.ref}.zip"]
    else:
        urls = [f"{base}/heads/main.zip", f"{base}/heads/master.zip"]

    last_error = "unknown error"
    for archive_url in urls:
        try:
            _extract_zipball(archive_url, dest)
            return dest
        except Exception as exc:  # noqa: BLE001 — try the next candidate
            last_error = str(exc)
            logger.debug("zipball fetch failed for %s: %s", archive_url, exc)

    target = f"github.com/{parsed.owner}/{parsed.repo}"
    if parsed.ref:
        raise RuntimeError(
            f"Failed to fetch ref {parsed.ref!r} from {target}; it matched no branch "
            f"or tag ({last_error})."
        )
    raise RuntimeError(f"Failed to fetch {target}: {last_error}")


def _extract_zipball(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "CoderAI-skills-installer"})
    with urlopen(req, timeout=_FETCH_TIMEOUT_SEC) as resp:  # noqa: S310 — github.com only
        data = resp.read(_MAX_ARCHIVE_BYTES + 1)
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise RuntimeError("GitHub archive exceeds size limit (50 MiB).")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path) as zf:
            _safe_extract_zip(zf, dest)
        children = [p for p in dest.iterdir() if not p.name.startswith(".")]
        if len(children) == 1 and children[0].is_dir():
            nested = children[0]
            for item in nested.iterdir():
                shutil.move(str(item), str(dest / item.name))
            nested.rmdir()
    finally:
        tmp_path.unlink(missing_ok=True)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract *zf* into *dest*, refusing any entry that escapes the directory.

    Containment is checked with :meth:`Path.is_relative_to`, not a string prefix:
    ``str(target).startswith(str(dest))`` also accepts a sibling whose name merely
    begins with ``dest`` (``/tmp/x-evil`` for ``dest=/tmp/x``).
    """
    dest = dest.resolve()
    for info in zf.infolist():
        name = Path(info.filename)
        if name.is_absolute() or ".." in name.parts:
            raise RuntimeError(f"Refusing unsafe zip entry: {info.filename}")
        target = (dest / info.filename).resolve()
        if not target.is_relative_to(dest):
            raise RuntimeError(f"Refusing zip path escape: {info.filename}")
    zf.extractall(dest)


def install_skill_directory(
    candidate: SkillCandidate,
    *,
    dest_root: Path,
    name: Optional[str] = None,
    force: bool = False,
    scope: SkillScope = "project",
    source_label: str = "",
) -> InstallResult:
    """Copy one skill folder into ``dest_root/<name>/`` with ``SKILLS.md``."""
    skill_name = validate_skill_name(name or candidate.name)
    dest = dest_root / skill_name
    if dest.exists() and not force:
        return InstallResult(
            name=skill_name,
            dest=dest,
            scope=scope,
            status="skipped",
            message=f"Already installed at {dest} (use --force to overwrite).",
            source=source_label,
        )

    dest_root.mkdir(parents=True, exist_ok=True)
    if scope == "user":
        restrict_path(dest_root, OWNER_RWX)

    if dest.exists():
        shutil.rmtree(dest)

    shutil.copytree(
        candidate.source_dir,
        dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store"),
    )
    _normalize_skill_filename(dest)

    if skill_markdown_path(dest) is None and candidate.skill_file is not None:
        shutil.copy2(candidate.skill_file, dest / SKILLS_FILE_NAME)

    if skill_markdown_path(dest) is None:
        shutil.rmtree(dest, ignore_errors=True)
        return InstallResult(
            name=skill_name,
            dest=dest,
            scope=scope,
            status="error",
            message="Installed folder is missing SKILL.md / SKILLS.md.",
            source=source_label,
        )

    if scope == "user":
        restrict_path(dest, OWNER_RWX)

    return InstallResult(
        name=skill_name,
        dest=dest,
        scope=scope,
        status="installed",
        message=f"Installed to {dest}",
        source=source_label,
    )


def _normalize_skill_filename(skill_dir: Path) -> None:
    """Rename ecosystem ``SKILL.md`` → canonical ``SKILLS.md`` when needed."""
    canonical = skill_dir / SKILLS_FILE_NAME
    legacy = skill_dir / "SKILL.md"
    if canonical.is_file():
        if legacy.is_file() and canonical.resolve() != legacy.resolve():
            legacy.unlink()
        return
    if legacy.is_file():
        legacy.rename(canonical)


def install_from_source(
    source: str,
    *,
    scope: SkillScope = "project",
    project_root: str | Path = ".",
    name: Optional[str] = None,
    path: Optional[str] = None,
    ref: Optional[str] = None,
    force: bool = False,
    only: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> InstallReport:
    """Fetch ``source``, discover skills, and install into the chosen scope."""
    parsed = parse_skill_source(source, ref=ref)
    subpath = path or parsed.subpath
    report = InstallReport()
    dest_root = skills_dir_for_scope(scope, project_root)

    with tempfile.TemporaryDirectory(prefix="coderai-skills-") as tmp:
        tmp_path = Path(tmp)
        if parsed.kind == "github":
            tree = fetch_source_tree(parsed, tmp_path / "repo")
        else:
            tree = fetch_source_tree(parsed, tmp_path)

        candidates = discover_skill_candidates(tree, subpath=subpath)
        report.candidates = candidates
        if not candidates:
            raise FileNotFoundError(
                "No skills found. Expected a folder with SKILL.md or SKILLS.md, "
                "or a skills/ directory containing skill folders."
            )

        selected = candidates
        if only:
            wanted = {n.strip() for n in only if n.strip()}
            selected = [c for c in candidates if c.name in wanted]
            missing = wanted - {c.name for c in selected}
            if missing:
                raise FileNotFoundError(
                    f"Skill(s) not found in source: {', '.join(sorted(missing))}"
                )
        elif name and len(candidates) > 1:
            matched = [c for c in candidates if c.name == name]
            selected = matched or candidates[:1]
        elif name and len(candidates) == 1:
            selected = candidates

        if dry_run:
            for c in selected:
                install_name = (
                    validate_skill_name(name or c.name) if len(selected) == 1 and name else c.name
                )
                report.results.append(
                    InstallResult(
                        name=install_name,
                        dest=dest_root / install_name,
                        scope=scope,
                        status="skipped",
                        message="dry-run",
                        source=parsed.display,
                    )
                )
            return report

        for c in selected:
            override_name: Optional[str] = name if (name and len(selected) == 1) else None
            report.results.append(
                install_skill_directory(
                    c,
                    dest_root=dest_root,
                    name=override_name,
                    force=force,
                    scope=scope,
                    source_label=parsed.display,
                )
            )
    return report


def list_installed_skills(
    *,
    scope: Optional[SkillScope] = None,
    project_root: str | Path = ".",
) -> list[tuple[SkillScope, Skill]]:
    """Return ``(scope, Skill)`` pairs for installed skills."""
    out: list[tuple[SkillScope, Skill]] = []
    scopes: Iterable[SkillScope]
    if scope is None:
        scopes = ("project", "user")
    else:
        scopes = (scope,)

    for sc in scopes:
        root = skills_dir_for_scope(sc, project_root)
        source = "user" if sc == "user" else "local"
        for skill in discover_skills_in_directory(root, source=source):
            out.append((sc, skill))
    return out


def remove_skill(
    name: str,
    *,
    scope: SkillScope = "project",
    project_root: str | Path = ".",
) -> Path:
    """Delete an installed skill directory. Raises if missing."""
    skill_name = validate_skill_name(name)
    dest = skills_dir_for_scope(scope, project_root) / skill_name
    if not dest.is_dir():
        raise FileNotFoundError(f"Skill {skill_name!r} not found in {scope} scope ({dest}).")
    shutil.rmtree(dest)
    return dest
