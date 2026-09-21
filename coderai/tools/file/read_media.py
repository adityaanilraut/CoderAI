"""UnderstandImage tool — analyze a local image via a vision-capable model."""

from __future__ import annotations

import base64
import os
import pathlib
import uuid
from typing import Any

from coderai.tools.legacy.types import ToolResult, as_str

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
    on_rate_limit = getattr(context, "on_plugin_rate_limit_exceeded", None) or (
        context.get("on_plugin_rate_limit_exceeded") if isinstance(context, dict) else None
    )

    if on_process_start:
        on_process_start(activity_id, f"UnderstandImage: {p.name}")

    try:
        image_bytes = p.read_bytes()
        b64_data = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64_data}"

        client_factory = getattr(context, "create_openai_client", None) or (
            context.get("create_openai_client") if isinstance(context, dict) else None
        )
        client_info = client_factory() if callable(client_factory) else {}
        client = client_info.get("client") if isinstance(client_info, dict) else None
        model = (
            client_info.get("model")
            if isinstance(client_info, dict) and client_info.get("model")
            else (getattr(context, "model", None) or "gpt-5.6-luna")
        )

        # First attempt: Use the OpenAI-compatible client with multimodal message format
        if client is not None:
            try:
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ]
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                )
                content = ((resp.choices[0].message.content if resp.choices else "") or "").strip()
                if content:
                    return ToolResult(
                        ok=True,
                        name="UnderstandImage",
                        output=content,
                        metadata={"imagePath": str(p.resolve())},
                    )
            except Exception as llm_err:
                err_msg = str(llm_err)
                if "rate limit" in err_msg.lower() and on_rate_limit:
                    on_rate_limit("UnderstandImage")
                return _tool_error(f"Image analysis failed with model '{model}': {err_msg}")

        return _tool_error(
            f"Unable to analyze image '{p.name}'. Please ensure your model has vision capabilities and an API key is configured."
        )
    finally:
        if on_process_exit:
            on_process_exit(activity_id)


# --- CallableTool2 Implementation ---

import base64 as _base64
from io import BytesIO as _BytesIO
from pathlib import Path as _Path
from kaos.path import KaosPath as _KaosPath
from kosong.tooling import (
    CallableTool2 as _CallableTool2,
    ToolError as _ToolError,
    ToolOk as _ToolOk,
    ToolReturnValue as _ToolReturnValue,
)
from pydantic import BaseModel as _BaseModel, Field as _Field

from coderai.soul.agent import Runtime as _Runtime
from coderai.tools import SkipThisTool as _SkipThisTool
from coderai.tools.file.utils import (
    MEDIA_SNIFF_BYTES as _MEDIA_SNIFF_BYTES,
    FileType as _FileType,
    detect_file_type as _detect_file_type,
)
from coderai.tools.utils import load_desc as _load_desc
from coderai.utils.logging import logger as _logger
from coderai.utils.media_tags import wrap_media_part as _wrap_media_part
from coderai.utils.path import (
    is_within_workspace as _is_within_workspace,
    kaos_path_from_user_input as _kaos_path_from_user_input,
)
from coderai.wire.types import ImageURLPart as _ImageURLPart, VideoURLPart as _VideoURLPart

MAX_MEDIA_MEGABYTES = 100


def _to_data_url(mime_type: str, data: bytes) -> str:
    encoded = _base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(_BytesIO(data)) as image:
            image.load()
            return image.size
    except Exception:
        return None


class ReadMediaParams(_BaseModel):
    path: str = _Field(
        description=(
            "The path to the file to read. Absolute paths are required when reading files "
            "outside the working directory."
        )
    )


class ReadMediaFile(_CallableTool2[ReadMediaParams]):
    name: str = "ReadMediaFile"
    params: type[ReadMediaParams] = ReadMediaParams

    def __init__(self, runtime: _Runtime) -> None:
        llm = getattr(runtime, "llm", None)
        capabilities = getattr(llm, "capabilities", set()) if llm else set()
        if "image_in" not in capabilities and "video_in" not in capabilities:
            raise _SkipThisTool()

        description = _load_desc(
            _Path(__file__).parent / "read_media.md",
            {
                "MAX_MEDIA_MEGABYTES": MAX_MEDIA_MEGABYTES,
                "capabilities": capabilities,
            },
        )
        super().__init__(description=description)
        self._runtime = runtime
        builtin = getattr(runtime, "builtin_args", None)
        self._work_dir = getattr(builtin, "CODERAI_WORK_DIR", _KaosPath.cwd())
        self._additional_dirs = getattr(runtime, "additional_dirs", [])
        self._capabilities = capabilities

    async def _validate_path(self, path: _KaosPath) -> _ToolError | None:
        resolved_path = path.canonical()
        if (
            not _is_within_workspace(resolved_path, self._work_dir, self._additional_dirs)
            and not path.is_absolute()
        ):
            return _ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to read a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    async def _read_media(self, path: _KaosPath, file_type: _FileType) -> _ToolReturnValue:
        assert file_type.kind in ("image", "video")

        media_path = str(path)
        stat = await path.stat()
        size = stat.st_size
        if size == 0:
            return _ToolError(
                message=f"`{path}` is empty.",
                brief="Empty file",
            )
        if size > (MAX_MEDIA_MEGABYTES << 20):
            return _ToolError(
                message=(
                    f"`{path}` is {size} bytes, which exceeds the max "
                    f"{MAX_MEDIA_MEGABYTES}MB bytes for media files."
                ),
                brief="File too large",
            )

        match file_type.kind:
            case "image":
                data = await path.read_bytes()
                data_url = _to_data_url(file_type.mime_type, data)
                part = _ImageURLPart(image_url=_ImageURLPart.ImageURL(url=data_url))
                wrapped = _wrap_media_part(part, tag="image", attrs={"path": media_path})
                image_size = _extract_image_size(data)
            case "video":
                data = await path.read_bytes()
                data_url = _to_data_url(file_type.mime_type, data)
                part = _VideoURLPart(video_url=_VideoURLPart.VideoURL(url=data_url))
                wrapped = _wrap_media_part(part, tag="video", attrs={"path": media_path})
                image_size = None

        size_hint = f", original size {image_size[0]}x{image_size[1]}px" if image_size else ""
        return _ToolOk(
            output=wrapped,
            message=(
                f"Loaded {file_type.kind} file `{path}` "
                f"({file_type.mime_type}, {size} bytes{size_hint})."
            ),
        )

    async def __call__(self, params: ReadMediaParams) -> _ToolReturnValue:
        if not params.path:
            return _ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        try:
            p = _kaos_path_from_user_input(params.path)
            if err := await self._validate_path(p):
                return err
            p = p.canonical()

            if not await p.exists():
                return _ToolError(
                    message=f"`{params.path}` does not exist.",
                    brief="File not found",
                )
            if not await p.is_file():
                return _ToolError(
                    message=f"`{params.path}` is not a file.",
                    brief="Invalid path",
                )

            header = await p.read_bytes(_MEDIA_SNIFF_BYTES)
            file_type = _detect_file_type(str(p), header=header)
            if file_type.kind == "text":
                return _ToolError(
                    message=f"`{params.path}` is a text file. Use ReadFile to read text files.",
                    brief="Unsupported file type",
                )
            if file_type.kind == "unknown":
                return _ToolError(
                    message=f"`{params.path}` seems not readable as an image or video file.",
                    brief="File not readable",
                )

            if file_type.kind == "image" and "image_in" not in self._capabilities:
                return _ToolError(
                    message="The current model does not support image input.",
                    brief="Unsupported media type",
                )
            if file_type.kind == "video" and "video_in" not in self._capabilities:
                return _ToolError(
                    message="The current model does not support video input.",
                    brief="Unsupported media type",
                )

            return await self._read_media(p, file_type)
        except Exception as e:
            _logger.warning("ReadMediaFile failed: {path}: {error}", path=params.path, error=e)
            return _ToolError(
                message=f"Failed to read {params.path}. Error: {e}",
                brief="Failed to read file",
            )
