"""Vision tool for reading and encoding images for LLM vision APIs."""

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .base import Tool

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
    parameters_model = ReadImageParams
    is_read_only = True

    async def execute(self, path: str) -> Dict[str, Any]:
        """Read and base64-encode an image file.

        Args:
            path: Path to the image file

        Returns:
            Dictionary with base64 data and metadata, or error info
        """
        try:
            filepath = Path(path).resolve()

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

            # Check file size
            file_size = filepath.stat().st_size
            if file_size > MAX_IMAGE_SIZE:
                return {
                    "success": False,
                    "error": (
                        f"Image too large: {file_size / (1024*1024):.1f} MB "
                        f"(max {MAX_IMAGE_SIZE / (1024*1024):.0f} MB)"
                    ),
                }

            # Read and encode
            image_data = filepath.read_bytes()
            b64_data = base64.b64encode(image_data).decode("utf-8")

            return {
                "success": True,
                "image_data": b64_data,
                "mime_type": mime_type,
                "file_name": filepath.name,
                "file_size": file_size,
                "_vision": True,  # Flag for agent to detect vision content
            }

        except PermissionError:
            return {"success": False, "error": f"Permission denied: {path}"}
        except Exception as e:
            return {"success": False, "error": str(e)}
