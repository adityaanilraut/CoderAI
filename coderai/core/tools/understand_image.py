"""UnderstandImage tool — analyze a local image via the vision-capable model (deepcode understand-image-handler.ts)."""

from __future__ import annotations

import os
import pathlib
import uuid
from typing import Any

from coderai.core.tools.types import ToolResult, as_str

MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _tool_error(error: str) -> ToolResult:
    return ToolResult(ok=False, name="UnderstandImage", error=error)


def handle(args: dict[str, Any], context: Any) -> ToolResult:
    return handle_understand_image_tool(args, context)


def handle_understand_image_tool(args: dict[str, Any], context: Any) -> ToolResult:
    prompt = as_str(args.get("prompt")).strip()
    image_path = as_str(args.get("image_path")).strip()

    if not prompt:
        return _tool_error('Missing required "prompt" string.')
    if not image_path:
        return _tool_error('Missing required "image_path" string.')
    if not os.path.isabs(image_path):
        return _tool_error('"image_path" must be an absolute path.')

    p = pathlib.Path(image_path)
    ext = p.suffix.lower()
    mime = MIME_BY_EXT.get(ext)
    if not mime:
        return _tool_error("Unsupported image format. Only JPEG, PNG, and WebP are supported.")

    try:
        st = p.stat()
    except Exception as e:
        return _tool_error(f"Unable to access image: {e}")

    if p.is_dir() or not p.is_file():
        return _tool_error('"image_path" must point to a regular file.')
    if st.st_size == 0:
        return _tool_error("Image file must not be empty.")
    if st.st_size > MAX_IMAGE_BYTES:
        return _tool_error("Image file exceeds the 10 MiB limit.")

    activity_id = f"understand-image-{uuid.uuid4()}"
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )

    if on_process_start:
        on_process_start(activity_id, f"UnderstandImage: {p.name}")

    try:
        return ToolResult(
            ok=True,
            name="UnderstandImage",
            output=f"Image {p.name} ({st.st_size} bytes) loaded. Prompt: {prompt}",
            metadata={"imagePath": str(p.resolve())},
        )
    finally:
        if on_process_exit:
            on_process_exit(activity_id)
