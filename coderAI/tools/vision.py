"""Vision tool for reading and encoding images for LLM vision APIs."""

import asyncio
import base64
import logging
import mimetypes
import os
from typing import Any

from pydantic import BaseModel, Field

from coderAI.tools.base import Tool
from coderAI.tools.filesystem import ProjectPathError, _O_NOFOLLOW, resolve_under_project

logger = logging.getLogger(__name__)

# Supported image MIME types
SUPPORTED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}

# Maximum image file size (10 MB)
MAX_IMAGE_SIZE = 10 * 1024 * 1024


class ReadImageParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path to the image file")


class ReadImageTool(Tool):
    """Tool for reading images and encoding them for LLM vision analysis.

    Returns base64-encoded image data that the agent can pass to the LLM
    as a vision content block, enabling the model to "see" screenshots,
    diagrams, UI mockups, etc.
    """

    name = "read_image"
    description = (
        "Read an image file and return its base64-encoded content for visual analysis. "
        "Supports PNG, JPEG, GIF, and WebP. Use this when you need to look at "
        "screenshots, diagrams, UI mockups, or any visual content."
    )
    category = "vision"
    parameters_model = ReadImageParams
    is_read_only = True

    async def execute(self, path: str) -> dict[str, Any]:  # type: ignore[override]
        """Read and base64-encode an image file.

        Args:
            path: Path to the image file

        Returns:
            Dictionary with base64 data and metadata, or error info
        """
        try:
            filepath = resolve_under_project(
                path,
                operation="read_image",
                check_protected=True,
                reject_symlink=True,
            )

            # Check file exists
            if not filepath.is_file():
                return {"success": False, "error": f"File not found: {path}"}

            # Check MIME type
            mime_type, _ = mimetypes.guess_type(str(filepath))
            if mime_type not in SUPPORTED_MIME_TYPES:
                return {
                    "success": False,
                    "error": (
                        f"Unsupported image type: {mime_type or 'unknown'}. "
                        f"Supported: {', '.join(sorted(SUPPORTED_MIME_TYPES))}"
                    ),
                }

            def _read() -> tuple[bytes, int]:
                fd = os.open(str(filepath), os.O_RDONLY | _O_NOFOLLOW)
                try:
                    file_size = os.fstat(fd).st_size
                    return os.read(fd, MAX_IMAGE_SIZE + 1), file_size
                finally:
                    os.close(fd)

            image_data, file_size = await asyncio.to_thread(_read)
            if file_size > MAX_IMAGE_SIZE:
                return {
                    "success": False,
                    "error": (
                        f"Image too large: {file_size / (1024 * 1024):.1f} MB "
                        f"(max {MAX_IMAGE_SIZE / (1024 * 1024):.0f} MB)"
                    ),
                }

            if len(image_data) > MAX_IMAGE_SIZE:
                return {
                    "success": False,
                    "error": "Image grew beyond the 10 MB limit while reading.",
                }

            b64_data = base64.b64encode(image_data).decode("utf-8")

            return {
                "success": True,
                "image_data": b64_data,
                "mime_type": mime_type,
                "file_name": filepath.name,
                "file_size": file_size,
                "_vision": True,  # Flag for agent to detect vision content
            }

        except ProjectPathError as e:
            return e.as_result()
        except PermissionError:
            return {"success": False, "error": f"Permission denied: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
