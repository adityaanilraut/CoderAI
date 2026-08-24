"""Image attachment helper for CoderAI CLI REPL."""

from __future__ import annotations

import base64
import os
import pathlib
import struct
from typing import Any

SUPPORTED_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB limit


def detect_image_dimensions(data: bytes, media_type: str) -> tuple[int, int]:
    """Extract intrinsic image dimensions (width, height) without external heavy dependencies."""
    try:
        if media_type == "image/png" and len(data) >= 24:
            # PNG IHDR chunk at offset 16: width (4 bytes), height (4 bytes)
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", data[16:24])
                return int(w), int(h)
        elif media_type == "image/gif" and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        elif media_type == "image/jpeg" and len(data) > 2:
            # Parse JPEG SOF segments
            idx = 2
            while idx < len(data) - 8:
                if data[idx] != 0xFF:
                    idx += 1
                    continue
                marker = data[idx + 1]
                # SOF0 (0xC0) to SOF3 (0xC3), SOF5 (0xC5) to SOF7 (0xC7), SOF9 (0xC9) to SOF11 (0xCB), SOF13 (0xCD) to SOF15 (0xCF)
                if marker in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                ):
                    h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                    return int(w), int(h)
                # Next segment length
                seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                idx += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def resolve_image_path(raw_path: str, project_root: str) -> pathlib.Path:
    """Resolve an image path relative to workspace or user home directory."""
    raw = os.path.expanduser(raw_path.strip().strip("'\""))
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = pathlib.Path(project_root) / path
    return path.resolve()


def parse_and_attach_image(
    file_path: str, project_root: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate image file, read bytes, and build OpenAI-compatible multimodal contentParam.

    Returns:
        (content_param, error_message)
    """
    resolved = resolve_image_path(file_path, project_root)
    if not resolved.is_file():
        return None, f"Image file not found: {file_path} (resolved: {resolved})"

    ext = resolved.suffix.lower()
    media_type = SUPPORTED_IMAGE_EXTENSIONS.get(ext)
    if not media_type:
        supported = ", ".join(SUPPORTED_IMAGE_EXTENSIONS.keys())
        return None, f"Unsupported image extension '{ext}'. Supported: {supported}"

    file_size = resolved.stat().st_size
    if file_size > MAX_IMAGE_BYTES:
        mb = file_size / (1024 * 1024)
        return None, f"Image file is too large ({mb:.1f}MB). Maximum allowed is 20MB."

    try:
        with open(resolved, "rb") as f:
            data = f.read()
    except Exception as err:
        return None, f"Failed to read image file: {err}"

    b64_data = base64.b64encode(data).decode("utf-8")
    data_url = f"data:{media_type};base64,{b64_data}"
    width, height = detect_image_dimensions(data, media_type)

    param = {
        "type": "image_url",
        "image_url": {"url": data_url},
        "name": resolved.name,
        "bytes": file_size,
        "media_type": media_type,
        "width": width,
        "height": height,
        "file_path": str(resolved),
    }
    return param, None
