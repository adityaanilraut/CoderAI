"""Project scaffolding application service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from coderAI.tui.controller import UIBridge


async def _cmd_init_project(server: UIBridge, _msg: dict[str, Any]) -> None:
    project_root = Path(getattr(server.agent.config, "project_root", ".")).resolve()
    # Scaffolding is blocking filesystem I/O (mkdir + write_text) — run it off
    # the event loop so the TUI stays responsive.
    created_dirs, created_files, skipped_files, error = await asyncio.to_thread(
        _do_init_project, project_root
    )
    if error is not None:
        server._emit_error("tool", error)
        return

    lines = [f"Scaffolded .coderAI/ in {project_root.name}:"]
    if created_dirs:
        lines.append(f"  {len(created_dirs)} directories created")
    for f in created_files:
        lines.append(f"  created: {f}")
    for f in skipped_files:
        lines.append(f"  skipped (exists): {f}")
    server.emit("success", message="\n".join(lines))


def _do_init_project(
    project_root: Path,
) -> tuple[list[str], list[str], list[str], Optional[str]]:
    """Blocking filesystem scaffolding for ``/init`` (runs off the event loop).

    Returns ``(created_dirs, created_files, skipped_files, error)``. On the
    first mkdir/write failure it returns early with a human-readable ``error``
    message (the async caller emits it); otherwise ``error`` is ``None``.
    """
    dot_dir = project_root / ".coderAI"
    from coderAI.assets.manifest import asset_text

    created_dirs: list[str] = []
    created_files: list[str] = []
    skipped_files: list[str] = []

    dirs_to_create = [
        dot_dir / "agents",
        dot_dir / "skills",
        dot_dir / "rules",
    ]

    for d in dirs_to_create:
        try:
            d.mkdir(parents=True, exist_ok=True)
            created_dirs.append(str(d.relative_to(project_root)))
        except OSError as e:
            return created_dirs, created_files, skipped_files, f"Cannot create {d.name}: {e}"

    try:
        files_to_create: list[tuple[Path, str]] = [
            (
                project_root / "CODERAI.md",
                asset_text("starter", "CODERAI.md"),
            ),
            (
                dot_dir / "agents" / "planner.md",
                asset_text("agents", "planner.md"),
            ),
            (
                dot_dir / "rules" / "001-common-principles.md",
                asset_text("rules", "001-common-principles.md"),
            ),
            (
                dot_dir / "tasks.json",
                "[]\n",
            ),
        ]
    except OSError as e:
        return (
            created_dirs,
            created_files,
            skipped_files,
            f"Cannot load packaged starter assets: {e}",
        )

    for filepath, content in files_to_create:
        rel = str(filepath.relative_to(project_root))
        if filepath.exists():
            skipped_files.append(rel)
            continue
        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            created_files.append(rel)
        except OSError as e:
            return created_dirs, created_files, skipped_files, f"Cannot write {rel}: {e}"

    return created_dirs, created_files, skipped_files, None
