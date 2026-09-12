"""Git repository context for explore subagents.

Collects remote/branch/dirty-files/recent-commits into a ``<git-context>``
block. Every git command is individually guarded with a short timeout — a
single failure (or non-repo cwd) yields ``""``, never an exception. stdlib
``subprocess`` only; no kaos dependency.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

TIMEOUT = 5.0
MAX_DIRTY_FILES = 20

_ALLOWED_HOSTS = (
    "github.com",
    "gitlab.com",
    "gitee.com",
    "bitbucket.org",
    "codeberg.org",
    "sr.ht",
)


async def collect_git_context(work_dir: str) -> str:
    """Collect git context for the explore agent (``""`` when unavailable)."""
    cwd = str(work_dir)
    if await _run_git(["rev-parse", "--is-inside-work-tree"], cwd) is None:
        return ""

    remote_url, branch, dirty_raw, log_raw = await asyncio.gather(
        _run_git(["remote", "get-url", "origin"], cwd),
        _run_git(["branch", "--show-current"], cwd),
        _run_git(["status", "--porcelain"], cwd),
        _run_git(["log", "-3", "--format=%h %s"], cwd),
    )

    sections: list[str] = [f"Working directory: {cwd}"]

    if remote_url:
        safe_url = _sanitize_remote_url(remote_url)
        if safe_url:
            sections.append(f"Remote: {safe_url}")
        project = _parse_project_name(remote_url)
        if project:
            sections.append(f"Project: {project}")

    if branch:
        sections.append(f"Branch: {branch}")

    if dirty_raw is not None:
        dirty_lines = [line for line in dirty_raw.splitlines() if line.strip()]
        if dirty_lines:
            total = len(dirty_lines)
            body = "\n".join(f"  {line}" for line in dirty_lines[:MAX_DIRTY_FILES])
            if total > MAX_DIRTY_FILES:
                body += f"\n  ... and {total - MAX_DIRTY_FILES} more"
            sections.append(f"Dirty files ({total}):\n{body}")

    if log_raw:
        log_lines = [line for line in log_raw.splitlines() if line.strip()]
        if log_lines:
            sections.append("Recent commits:\n" + "\n".join(f"  {l[:200]}" for l in log_lines))

    if len(sections) <= 1:
        return ""
    return "<git-context>\n" + "\n".join(sections) + "\n</git-context>"


async def _run_git(args: list[str], cwd: str, timeout: float = TIMEOUT) -> str | None:
    """Run one git command; ``None`` on any failure/timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
        if proc.returncode != 0:
            return None
        return (stdout or b"").decode("utf-8", errors="replace").strip() or None
    except (OSError, ValueError):
        return None


def _sanitize_remote_url(remote_url: str) -> str | None:
    """Public-host remote URL with credentials stripped, else None."""
    for host in _ALLOWED_HOSTS:
        if re.match(rf"^git@{re.escape(host)}:", remote_url):
            return remote_url
    try:
        parsed = urlparse(remote_url)
        if parsed.hostname not in _ALLOWED_HOSTS:
            return None
        safe = parsed._replace(netloc=parsed.hostname)
        return safe.geturl()
    except ValueError:
        return None


def _parse_project_name(remote_url: str) -> str | None:
    """Best-effort ``owner/repo`` from a remote URL."""
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", remote_url.strip())
    return match.group(1) if match else None
