"""Draft attachment navigation and removal without submitting the draft."""

from __future__ import annotations

import asyncio
from typing import Any

from coderai.ui.shell.interaction import BrowserRow
from coderai.ui.shell.prompt import FILE_MENTION_PATTERN, _find_matching_file, _parse_line_range


def file_references(text: str):
    return [m for m in FILE_MENTION_PATTERN.finditer(text) if not m[1].startswith("session:")]


def describe_file(reference: str, root: str) -> str:
    try:
        path, start, end = _parse_line_range(reference)
        resolved = _find_matching_file(root, path)
        if resolved is None:
            return f"Missing file: {path}"
        scope = f" | lines {start}-{end}" if start else ""
        return f"{resolved}{scope} | {resolved.stat().st_size:,} bytes"
    except (ValueError, OSError) as exc:
        return str(exc)


def preview_files(text: str, root: str) -> list[str]:
    return [describe_file(match[1], root) for match in file_references(text)]


async def manage_attachments(controller: Any, args: str) -> None:
    view = controller.view
    references = file_references(view.draft)
    images = view.attachments

    def remove(index: int) -> None:
        if index < 0 or index >= len(images) + len(references):
            raise ValueError("Attachment index is out of range.")
        if index < len(images):
            images.pop(index)
        else:
            match = references[index - len(images)]
            view.draft = view.draft[: match.start()] + view.draft[match.end() :]
        controller.file_preview.clear()

    if args == "edit":
        rows = [
            BrowserRow(
                str(i),
                f"Remove image: {image.get('file_path', image.get('name', 'image'))}",
                f"{image.get('bytes', 0):,} bytes | {image.get('width', '?')} x {image.get('height', '?')}",
            )
            for i, image in enumerate(images)
        ]
        descriptions = await asyncio.to_thread(
            preview_files, view.draft, controller.mgr.project_root
        )
        rows.extend(
            BrowserRow(str(len(images) + i), f"Remove file: {match[1]}", descriptions[i])
            for i, match in enumerate(references)
        )
        selected = await controller.select("Draft attachments", rows)
        if not selected.cancelled:
            remove(int(selected.value))
        return
    if args.startswith("remove "):
        parts = args.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError("Usage: /attachments remove <number>")
        remove(int(parts[1]) - 1)
    elif args == "clear":
        images.clear()
        for match in reversed(references):
            view.draft = view.draft[: match.start()] + view.draft[match.end() :]
        controller.file_preview.clear()
    elif args:
        raise ValueError("Usage: /attachments [edit|remove <number>|clear]")
    for i, image in enumerate(images):
        controller.presentation.emit(
            f"{i + 1}. {image.get('file_path', image.get('name', 'image'))} | {image.get('bytes', 0):,} bytes"
        )
    descriptions = await asyncio.to_thread(preview_files, view.draft, controller.mgr.project_root)
    for i, description in enumerate(descriptions, start=len(images) + 1):
        controller.presentation.emit(f"{i}. {description}")
    if not images and not descriptions:
        controller.presentation.emit(
            "No draft attachments. Ctrl-T opens this tray from the composer."
        )
