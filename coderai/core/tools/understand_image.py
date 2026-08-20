"""UnderstandImage tool — analyze a local image via the vision-capable model (deepcode understand-image-handler.ts)."""

from __future__ import annotations

import base64
import os
import pathlib
import uuid
from typing import Any

from coderai.core.tools.types import ToolResult, as_str

DEFAULT_UNDERSTAND_IMAGE_API_URL = "https://deepcode.vegamo.cn/api/plugin/understand-image"
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

        # 1. First attempt: Use the OpenAI-compatible client with multimodal message format
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

        # 2. Fallback attempt: If machineId/plusApiKey or plugin endpoint is available
        machine_id = client_info.get("machineId") if isinstance(client_info, dict) else None
        plus_api_key = client_info.get("plusApiKey") if isinstance(client_info, dict) else None
        if machine_id or plus_api_key:
            try:
                import requests

                headers = {}
                if machine_id:
                    headers["Token"] = machine_id
                if plus_api_key:
                    headers["PLUS-API-KEY"] = plus_api_key

                files = {"image": (p.name, image_bytes, mime)}
                data = {"prompt": prompt}
                response = requests.post(
                    DEFAULT_UNDERSTAND_IMAGE_API_URL,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=30,
                )
                if response.ok:
                    payload = response.json()
                    if payload.get("success") is True and payload.get("result"):
                        return ToolResult(
                            ok=True,
                            name="UnderstandImage",
                            output=str(payload["result"]).strip(),
                            metadata={"imagePath": str(p.resolve())},
                        )
                    if "rate limit" in str(payload.get("reason", "")).lower() and on_rate_limit:
                        on_rate_limit("UnderstandImage")
            except Exception:
                pass

        return _tool_error(
            f"Unable to analyze image '{p.name}'. Please ensure your model has vision capabilities or an API key is configured."
        )
    finally:
        if on_process_exit:
            on_process_exit(activity_id)
