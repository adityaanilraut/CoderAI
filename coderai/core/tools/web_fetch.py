"""WebFetch tool — fetch and sanitize online web pages, documentation, and APIs."""

from __future__ import annotations

import json
import uuid
from typing import Any

from coderai.core.network.client import get_http_client
from coderai.core.network.sanitizer import extract_and_sanitize_html, slice_payload
from coderai.core.network.security import NetworkSecurityError
from coderai.core.tools.types import ToolResult, as_str

WEB_FETCH_ACTIVITY_PREFIX = "WebFetch:"
MAX_OUTPUT_CHARS = 30_000


def _format_activity_label(url: str) -> str:
    max_len = 120
    clipped = f"{url[: max_len - 3]}..." if len(url) > max_len else url
    return f"{WEB_FETCH_ACTIVITY_PREFIX} {clipped}"


async def handle_web_fetch_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Fetch content from an external web URL, sanitize against prompt injection, and return clean Markdown."""
    url = as_str(args.get("url")).strip()
    if not url:
        return ToolResult(ok=False, name="WebFetch", error='Missing required "url" argument.')

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    raw_mode = bool(args.get("raw", False))
    max_length = (
        int(args.get("max_length", MAX_OUTPUT_CHARS))
        if args.get("max_length") is not None
        else MAX_OUTPUT_CHARS
    )
    use_cache = bool(args.get("use_cache", True))

    # Activity tracking hooks
    activity_id = f"web-fetch-{uuid.uuid4()}"
    on_process_start = getattr(context, "on_process_start", None) or (
        context.get("on_process_start") if isinstance(context, dict) else None
    )
    on_process_exit = getattr(context, "on_process_exit", None) or (
        context.get("on_process_exit") if isinstance(context, dict) else None
    )

    if on_process_start:
        on_process_start(activity_id, _format_activity_label(url))

    try:
        # Resolve network policy from context/settings if available
        client = get_http_client()

        resp = await client.get_async(
            url,
            timeout=(10.0, 30.0),
            use_cache=use_cache,
            cache_ttl=300.0,
        )

        if not resp.ok:
            error_detail = resp.error or f"HTTP {resp.status_code}"
            return ToolResult(
                ok=False,
                name="WebFetch",
                error=f"Failed to fetch '{url}': {error_detail}",
                metadata={"url": url, "statusCode": resp.status_code, "elapsedMs": resp.elapsed_ms},
            )

        content_type = resp.headers.get("content-type", "").lower()

        # Handle JSON responses
        if "application/json" in content_type:
            try:
                parsed_json = json.loads(resp.text)
                formatted = json.dumps(parsed_json, indent=2)
                sliced, truncated = slice_payload(formatted, max_chars=max_length)
                return ToolResult(
                    ok=True,
                    name="WebFetch",
                    output=sliced,
                    metadata={
                        "url": resp.url,
                        "contentType": "application/json",
                        "statusCode": resp.status_code,
                        "fromCache": resp.from_cache,
                        "truncated": truncated,
                    },
                )
            except Exception:
                pass

        # Handle Plain Text responses
        if raw_mode or "text/plain" in content_type:
            sliced, truncated = slice_payload(resp.text, max_chars=max_length)
            return ToolResult(
                ok=True,
                name="WebFetch",
                output=sliced,
                metadata={
                    "url": resp.url,
                    "contentType": "text/plain",
                    "statusCode": resp.status_code,
                    "fromCache": resp.from_cache,
                    "truncated": truncated,
                },
            )

        # Handle HTML responses: extract metadata and convert to clean Markdown with sanitization
        extracted = extract_and_sanitize_html(resp.text, max_chars=max_length, base_url=resp.url)

        output_parts: list[str] = []
        if extracted.title:
            output_parts.append(f"# {extracted.title}\n")
        if extracted.description:
            output_parts.append(f"> {extracted.description}\n")

        output_parts.append(extracted.markdown)
        final_output = "\n".join(output_parts).strip()

        return ToolResult(
            ok=True,
            name="WebFetch",
            output=final_output,
            metadata={
                "url": resp.url,
                "title": extracted.title,
                "description": extracted.description,
                "canonicalUrl": extracted.canonical_url,
                "author": extracted.author,
                "statusCode": resp.status_code,
                "fromCache": resp.from_cache,
                "totalChars": extracted.total_chars,
                "truncated": extracted.truncated,
                "elapsedMs": round(resp.elapsed_ms, 2),
            },
        )

    except NetworkSecurityError as sec_err:
        return ToolResult(
            ok=False,
            name="WebFetch",
            error=f"Security Policy Violation: {sec_err}",
            metadata={"url": url, "securityBlocked": True},
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            name="WebFetch",
            error=f"Unexpected error fetching '{url}': {exc}",
            metadata={"url": url},
        )
    finally:
        if on_process_exit:
            on_process_exit(activity_id)


# Alias for backward compatibility
handle = handle_web_fetch_tool
