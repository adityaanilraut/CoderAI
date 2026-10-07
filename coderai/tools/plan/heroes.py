"""Session-owned plan file paths and approval reads."""

from __future__ import annotations

from pathlib import Path

from coderai.share import get_share_dir

PLANS_DIR = get_share_dir() / "plans"


def get_plan_file_path(
    session_id: str, project_root: str | Path | None = None, *, create: bool = True
) -> Path:
    """Get the plan file path for the given session.

    When project_root is provided (or when working inside a project), the plan lives
    in ``<project_root>/.coderai/plans/<session_id>.md`` so it is inside the workspace.
    Otherwise falls back to ``~/.coderai/plans/<session_id>.md``.
    """
    if not session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("Plan file requires a valid runtime session identity")
    if project_root:
        base = Path(project_root).resolve() / ".coderai" / "plans"
    else:
        base = PLANS_DIR
    try:
        if create:
            base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base / f"{session_id}.md"


def read_plan_file(session_id: str, project_root: str | Path | None = None) -> str | None:
    """Read the plan file content for the given session, or None if not found."""
    path = get_plan_file_path(session_id, project_root=project_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def read_plan_for_approval(session_id: str, project_root: str) -> str | None:
    """Present the complete owned plan; oversized or redirected files cannot be approved."""
    path = get_plan_file_path(session_id, project_root, create=False)
    if not path.exists() and not path.is_symlink():
        return None
    if path.resolve() != path.absolute():
        raise ValueError("Plan file or directory is a symlink; use the owned plan file")
    from coderai.utils.path import open_regular_binary

    with open_regular_binary(str(path)) as stream:
        content = stream.read(50 * 1024 + 1)
    if len(content) > 50 * 1024:
        raise ValueError(
            "Plan exceeds the 50 KiB presentation limit; shorten it before requesting acceptance"
        )
    return content.decode("utf-8", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
