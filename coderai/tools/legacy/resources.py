"""Fair resource conflict scheduling shared by tools on one event loop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import weakref
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ResourceAccess:
    kind: Literal["file", "all"] = "file"
    operation: Literal["read", "write", "readwrite", "search"] = "read"
    path: str = ""
    recursive: bool = False


def accesses_conflict(left: Sequence[ResourceAccess], right: Sequence[ResourceAccess]) -> bool:
    for first in left:
        for second in right:
            if first.kind == "all" or second.kind == "all":
                return True
            if first.operation in ("read", "search") and second.operation in ("read", "search"):
                continue
            first_path = os.path.normcase(os.path.normpath(first.path))
            second_path = os.path.normcase(os.path.normpath(second.path))
            if first_path == second_path:
                return True
            if first.recursive and second_path.startswith(first_path.rstrip(os.sep) + os.sep):
                return True
            if second.recursive and first_path.startswith(second_path.rstrip(os.sep) + os.sep):
                return True
    return False


class ResourceScheduler:
    """Let unrelated work pass, while queued conflicting writers retain priority."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active: list[tuple[object, tuple[ResourceAccess, ...]]] = []
        self._queued: list[tuple[object, tuple[ResourceAccess, ...]]] = []

    @contextlib.asynccontextmanager
    async def acquire(self, accesses: Sequence[ResourceAccess]) -> AsyncIterator[None]:
        request = (object(), tuple(accesses))
        async with self._condition:
            self._queued.append(request)
            try:
                await self._condition.wait_for(
                    lambda: (
                        not any(
                            accesses_conflict(request[1], candidate[1])
                            for candidate in (
                                self._active + self._queued[: self._queued.index(request)]
                            )
                        )
                    )
                )
            except BaseException:
                self._queued.remove(request)
                self._condition.notify_all()
                raise
            self._queued.remove(request)
            self._active.append(request)
            self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._active.remove(request)
                self._condition.notify_all()


_schedulers: weakref.WeakKeyDictionary[Any, ResourceScheduler] = weakref.WeakKeyDictionary()


def get_resource_scheduler() -> ResourceScheduler:
    loop = asyncio.get_running_loop()
    scheduler = _schedulers.get(loop)
    if scheduler is None:
        scheduler = ResourceScheduler()
        _schedulers[loop] = scheduler
    return scheduler


def tool_resource_accesses(
    tool: Any, args: dict[str, Any], context: Any
) -> tuple[ResourceAccess, ...]:
    from coderai.tools.file.utils import get_effective_workdir

    declared = getattr(tool, "resource_accesses", None)
    if declared is not None:
        return tuple(declared(args, context) if callable(declared) else declared)
    name = tool.name.lower()
    workdir = get_effective_workdir(context)
    raw_path = args.get("file_path") or args.get("path") or args.get("target_file")
    if name == "edit" and not raw_path and args.get("snippet_id"):
        from coderai.file_snippets import get_snippet

        snippet = get_snippet(context.session_id, args["snippet_id"])
        raw_path = snippet.file_path if snippet else None

    if name in ("glob", "grep"):
        raw_path = raw_path or workdir
        operation: Literal["read", "write", "readwrite", "search"] = "search"
        recursive = True
    elif name in ("read", "read_file", "read_media_file"):
        operation = "read"
        recursive = False
    elif name in ("write", "edit", "str_replace_editor", "patch", "apply_patch"):
        operation = (
            "read"
            if name == "str_replace_editor" and args.get("command") == "view"
            else "readwrite"
        )
        recursive = False
    else:
        if getattr(tool, "category", None) in ("subagent", "web", "interactive"):
            return ()
        safe = getattr(tool, "check_concurrency_safe", None)
        if callable(safe) and safe(args):
            return ()
        return (ResourceAccess(kind="all"),)

    if not isinstance(raw_path, str) or not raw_path:
        return (ResourceAccess(kind="all"),)
    path = pathlib.Path(raw_path).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(workdir) / path
    path = path.resolve()
    if operation == "read":
        if not path.exists() and name in ("read", "read_file"):
            path = pathlib.Path(workdir).resolve()
            recursive = True
        elif path.is_dir():
            recursive = True
    return (ResourceAccess(operation=operation, path=str(path), recursive=recursive),)


def external_resource_accesses(name: str, context: Any) -> tuple[ResourceAccess, ...]:
    """Only trusted local declarations can relax external conflict scheduling."""
    from coderai.config import resolve_current_settings

    manager = getattr(context, "session_manager", None)
    settings = (
        manager.get_resolved_settings()
        if manager and hasattr(manager, "get_resolved_settings")
        else resolve_current_settings(getattr(context, "project_root", "."))
    )
    policy = (settings.get("toolPolicies") or {}).get(name, {})
    resources = policy.get("resources") if isinstance(policy, dict) else None
    if not isinstance(resources, list) or not resources:
        return (ResourceAccess(kind="all"),)
    root = pathlib.Path(getattr(context, "project_root", ".")).resolve()
    result = []
    for resource in resources:
        if (
            not isinstance(resource, dict)
            or resource.get("operation") not in {"read", "write", "readwrite", "search"}
            or not isinstance(resource.get("path"), str)
        ):
            return (ResourceAccess(kind="all"),)
        path = (root / resource["path"]).resolve()
        if not path.is_relative_to(root):
            return (ResourceAccess(kind="all"),)
        if policy.get("effects") == "read" and resource["operation"] not in {"read", "search"}:
            return (ResourceAccess(kind="all"),)
        result.append(
            ResourceAccess(
                operation=resource["operation"],
                path=str(path),
                recursive=bool(resource.get("recursive")),
            )
        )
    return tuple(result)
