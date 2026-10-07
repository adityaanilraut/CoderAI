"""Fetch bounded public web content and extract readable text for terminal agents."""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from coderai.network.security import NetworkPolicy, NetworkSecurityError
from coderai.tools.legacy.types import ToolResult, as_str
from coderai.tools.web.common import (
    DEFAULT_OUTPUT_CHARS,
    EXTERNAL_CONTENT_NOTICE,
    MAX_OUTPUT_CHARS as HARD_OUTPUT_LIMIT,
    bounded_int,
    safe_web_url,
    sanitize_prompt_injection,
    slice_payload,
    web_settings,
)
from coderai.utils.aiohttp import HttpClient, get_http_client

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
DROP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "frame",
    "object",
    "embed",
    "applet",
    "form",
    "input",
    "button",
    "select",
    "option",
    "textarea",
    "nav",
    "footer",
    "aside",
}
BLOCK_TAGS = {"p", "div", "article", "section", "main", "header", "blockquote", "dl", "dt", "dd"}
WEB_FETCH_ACTIVITY_PREFIX = "WebFetch:"


@dataclass
class ExtractedWebPage:
    title: str = ""
    description: str = ""
    author: str = ""
    canonical_url: str = ""
    markdown: str = ""
    raw_text: str = ""
    total_chars: int = 0
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class _HTMLToMarkdownParser(HTMLParser):
    """HTML extraction with balanced hidden subtrees, links, lists, and tables."""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.drop_stack: list[str] = []
        self.output_chunks: list[str] = []
        self.metadata: dict[str, str] = {}
        self.in_title = False
        self.title_text: list[str] = []
        self.in_pre = False
        self.current_link_url: str | None = None
        self.current_link_text: list[str] = []
        self.list_counters: list[int] = []
        self.tables: list[dict[str, Any]] = []

    def _target(self) -> list[str]:
        if self.current_link_url:
            return self.current_link_text
        if self.tables and self.tables[-1]["cell"] is not None:
            return self.tables[-1]["cell"]
        return self.output_chunks

    def _emit(self, text: str) -> None:
        self._target().append(text)

    def _newline(self, count: int = 2) -> None:
        chunks = self._target()
        if chunks:
            tail = "".join(chunks[-3:])
            self._emit("\n" * max(0, count - (len(tail) - len(tail.rstrip("\n")))))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        style = re.sub(r"\s+", "", attrs_map.get("style", "").lower())
        hidden = (
            "hidden" in attrs_map
            or attrs_map.get("aria-hidden", "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if self.drop_stack or tag in DROP_TAGS or hidden:
            if tag not in VOID_TAGS:
                self.drop_stack.append(tag)
            return
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = attrs_map.get("name") or attrs_map.get("property")
            if key:
                self.metadata[key.lower()] = attrs_map.get("content", "")
        elif tag == "link" and "canonical" in attrs_map.get("rel", "").lower().split():
            self.metadata["canonical"] = safe_web_url(attrs_map.get("href", ""), self.base_url)
        elif re.fullmatch(r"h[1-6]", tag):
            self._newline()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in BLOCK_TAGS:
            self._newline()
            if tag == "blockquote":
                self._emit("> ")
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._newline()
            self._emit("---\n\n")
        elif tag == "pre":
            self._newline()
            self.in_pre = True
            self._emit("\x60\x60\x60\n")
        elif tag == "code" and not self.in_pre:
            self._emit("\x60")
        elif tag in {"ul", "ol"}:
            self._newline(1)
            self.list_counters.append(1 if tag == "ol" else 0)
        elif tag == "li":
            self._newline(1)
            count = self.list_counters[-1] if self.list_counters else 0
            self._emit(
                "  " * max(0, len(self.list_counters) - 1) + (f"{count}. " if count else "- ")
            )
            if count:
                self.list_counters[-1] += 1
        elif tag == "a":
            url = safe_web_url(attrs_map.get("href", ""), self.base_url)
            if url and not self.current_link_url:
                self.current_link_url = url
                self.current_link_text = []
        elif tag in {"strong", "b", "em", "i", "del", "s"}:
            self._emit(
                {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~"}[tag]
            )
        elif tag == "table":
            self._newline()
            self.tables.append({"rows": [], "row": [], "cell": None})
        elif self.tables and tag == "tr":
            self.tables[-1]["row"] = []
        elif self.tables and tag in {"th", "td"}:
            self.tables[-1]["cell"] = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.drop_stack:
            if tag in self.drop_stack:
                index = len(self.drop_stack) - 1 - self.drop_stack[::-1].index(tag)
                del self.drop_stack[index:]
            return
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.current_link_url:
            url, label = self.current_link_url, "".join(self.current_link_text).strip()
            self.current_link_url = None
            self.current_link_text = []
            self._emit(f"[{label or url}]({url.replace('(', '%28').replace(')', '%29')})")
        elif tag == "pre":
            self._newline(1)
            self._emit("\x60\x60\x60\n\n")
            self.in_pre = False
        elif tag == "code" and not self.in_pre:
            self._emit("\x60")
        elif tag in {"strong", "b", "em", "i", "del", "s"}:
            self._emit(
                {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~"}[tag]
            )
        elif self.tables and tag in {"th", "td"}:
            table = self.tables[-1]
            if table["cell"] is not None:
                table["row"].append(" ".join("".join(table["cell"]).split()).replace("|", r"\|"))
                table["cell"] = None
        elif self.tables and tag == "tr":
            table = self.tables[-1]
            if table["row"]:
                table["rows"].append(table["row"])
                table["row"] = []
        elif self.tables and tag == "table":
            rows = self.tables.pop()["rows"]
            if rows:
                width = max(map(len, rows))
                formatted = [
                    "| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows
                ]
                formatted.insert(1, "| " + " | ".join(["---"] * width) + " |")
                self._emit("\n".join(formatted) + "\n\n")
        elif tag in {"ul", "ol"}:
            if self.list_counters:
                self.list_counters.pop()
            self._newline()
        elif tag == "li":
            self._newline(1)
        elif tag in BLOCK_TAGS or re.fullmatch(r"h[1-6]", tag):
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.drop_stack:
            return
        if self.in_title:
            self.title_text.append(data)
        else:
            self._emit(data if self.in_pre else re.sub(r"\s+", " ", data))

    def get_result(self) -> tuple[str, dict[str, str]]:
        if self.title_text:
            self.metadata["title"] = "".join(self.title_text).strip()
        return "".join(self.output_chunks), self.metadata


def clean_markdown_whitespace(text: str) -> str:
    """Collapse blank lines outside code fences, preserving examples verbatim."""
    lines: list[str] = []
    in_code = False
    blanks = 0
    for line in text.split("\n"):
        if line.lstrip().startswith("\x60\x60\x60"):
            in_code = not in_code
        if in_code:
            lines.append(line)
            continue
        line = line.rstrip()
        blanks = blanks + 1 if not line else 0
        if blanks <= 1:
            lines.append(line)
    return "\n".join(lines).strip()


def extract_and_sanitize_html(
    html_content: str,
    max_chars: int = DEFAULT_OUTPUT_CHARS,
    base_url: str = "",
) -> ExtractedWebPage:
    parser = _HTMLToMarkdownParser(base_url)
    parser.feed(html_content)
    parser.close()
    markdown, meta = parser.get_result()
    meta = {
        key[:128]: sanitize_prompt_injection(value)[:2000] for key, value in list(meta.items())[:64]
    }
    cleaned = clean_markdown_whitespace(sanitize_prompt_injection(markdown))
    sliced, truncated = slice_payload(cleaned, max_chars)
    return ExtractedWebPage(
        title=meta.get("title") or meta.get("og:title", ""),
        description=meta.get("description") or meta.get("og:description", ""),
        author=meta.get("author") or meta.get("article:author", ""),
        canonical_url=safe_web_url(meta.get("canonical") or meta.get("og:url", ""), base_url)
        or base_url,
        markdown=sliced,
        raw_text=cleaned,
        total_chars=len(cleaned),
        truncated=truncated,
        metadata=meta,
    )


def _select_section(text: str, section: str) -> str:
    headings = list(re.finditer(r"^(#{1,6})\s+(.+)$", text, re.M))
    for index, match in enumerate(headings):
        title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", match[2]).strip().rstrip("¶").strip()
        if title.casefold() == section.casefold().strip():
            end = len(text)
            for following in headings[index + 1 :]:
                if len(following[1]) <= len(match[1]):
                    end = following.start()
                    break
            return text[match.start() : end].strip()
    raise ValueError(f"Section '{section}' was not found; use a heading from the page.")


def _pdf_text(content: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Encrypted PDF requires a password.")
    if len(reader.pages) > 200:
        raise ValueError("PDF exceeds the 200-page extraction limit.")
    parts: list[str] = []
    total = 0
    has_text = False
    for number, page in enumerate(reader.pages, 1):
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > 10 * 1024 * 1024:
            raise ValueError("PDF page exceeds the decoded content limit.")
        extracted_text = page.extract_text() or ""
        has_text = has_text or bool(extracted_text.strip())
        part = f"## Page {number}\n\n{extracted_text}"
        total += len(part)
        if total > 2_000_000:
            raise ValueError("PDF exceeds the extracted text limit.")
        parts.append(part)
    if not has_text:
        raise ValueError("No readable text extracted. Scanned PDFs require OCR.")
    return "\n\n".join(parts), len(reader.pages)


async def handle_web_fetch_tool(args: dict[str, Any], context: Any) -> ToolResult:
    url = as_str(args.get("url")).strip()
    if not url:
        return ToolResult(ok=False, name="WebFetch", error='Missing required "url" argument.')
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    try:
        max_length = bounded_int(
            args.get("max_length"), DEFAULT_OUTPUT_CHARS, 1, HARD_OUTPUT_LIMIT, "max_length"
        )
        offset = bounded_int(args.get("offset"), 0, 0, 5_000_000, "offset")
    except ValueError as exc:
        return ToolResult(ok=False, name="WebFetch", error=str(exc))
    activity_id = f"web-fetch-{uuid.uuid4()}"
    get = context.get if isinstance(context, dict) else lambda key: getattr(context, key, None)
    on_start, on_exit = get("on_process_start"), get("on_process_exit")
    owned_client = None
    metadata: dict[str, Any] = {"url": url, "untrusted": True}
    if on_start:
        on_start(activity_id, f"{WEB_FETCH_ACTIVITY_PREFIX} {url[:120]}")
    try:
        settings = web_settings(context)
        if settings.get("network"):
            owned_client = HttpClient(policy=NetworkPolicy.from_settings(settings))
        client = owned_client or get_http_client()
        response = await client.get_async(
            url,
            timeout=(10.0, 30.0),
            use_cache=bool(args.get("use_cache", True)),
            cache_ttl=300.0,
        )
        metadata = {
            "url": response.url,
            "statusCode": response.status_code,
            "fromCache": response.from_cache,
            "elapsedMs": round(response.elapsed_ms, 2),
            "bytes": len(response.content),
            "fetchedAt": response.fetched_at,
            "cacheAgeSeconds": max(0, time.time() - response.fetched_at)
            if response.fetched_at
            else 0,
            "untrusted": True,
        }
        if not response.ok:
            if response.security_blocked:
                metadata["securityBlocked"] = True
            return ToolResult(
                ok=False,
                name="WebFetch",
                error=f"Failed to fetch '{url}': {response.error or f'HTTP {response.status_code}'}",
                metadata=metadata,
            )
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        metadata["contentType"] = content_type
        needs_sanitization = True
        if content_type == "application/pdf" or response.content.startswith(b"%PDF-"):
            text, pages = await asyncio.to_thread(_pdf_text, response.content)
            metadata["pages"] = pages
        elif args.get("raw"):
            if not (
                content_type.startswith("text/")
                or "json" in content_type
                or "xml" in content_type
                or not content_type
            ):
                raise ValueError(f"Unsupported content type: {content_type}")
            text = response.text
        elif content_type == "application/json" or content_type.endswith("+json"):
            text = json.dumps(json.loads(response.text), indent=2, ensure_ascii=False)
        elif content_type in {"text/html", "application/xhtml+xml", ""}:
            page = extract_and_sanitize_html(
                response.text, max_chars=HARD_OUTPUT_LIMIT, base_url=response.url
            )
            metadata.update(
                {
                    "title": page.title,
                    "description": page.description,
                    "canonicalUrl": page.canonical_url,
                    "author": page.author,
                }
            )
            text = "\n\n".join(
                part
                for part in (
                    f"# {page.title}" if page.title else "",
                    f"> {page.description}" if page.description else "",
                    page.raw_text,
                )
                if part
            )
            if not page.raw_text.strip():
                raise ValueError(
                    "No readable page content found. The page may require JavaScript, login, or access verification."
                )
            needs_sanitization = False
        elif content_type.startswith("text/") or content_type in {
            "application/xml",
            "application/rss+xml",
            "application/atom+xml",
        }:
            text = response.text
        else:
            raise ValueError(f"Unsupported content type: {content_type or 'unknown'}")
        if needs_sanitization:
            text = sanitize_prompt_injection(text)
        if args.get("section"):
            text = _select_section(text, as_str(args["section"]))
        if not text.strip():
            raise ValueError("No readable text extracted. Scanned PDFs require OCR.")
        if offset >= len(text) and offset:
            raise ValueError(f"offset exceeds the available {len(text)} characters.")
        metadata["totalChars"] = len(text)
        metadata["offset"] = offset
        notice = EXTERNAL_CONTENT_NOTICE + "\n\n"
        if max_length > len(notice) + 64:
            sliced, truncated = slice_payload(text[offset:], max_length - len(notice))
            output = notice + sliced
        else:
            sliced, truncated = slice_payload(text[offset:], max_length)
            output = sliced
        displayed = len(sliced)
        if truncated:
            for footer in (
                "\n\n[Content truncated; use offset to continue.]",
                "[Content truncated]",
            ):
                if sliced.endswith(footer):
                    displayed -= len(footer)
                    break
        metadata.update(
            {"truncated": truncated, "nextOffset": offset + displayed if truncated else None}
        )
        return ToolResult(ok=True, name="WebFetch", output=output, metadata=metadata)
    except NetworkSecurityError as exc:
        return ToolResult(
            ok=False,
            name="WebFetch",
            error=f"Security Policy Violation: {exc}",
            metadata={"url": url, "securityBlocked": True},
        )
    except Exception as exc:
        return ToolResult(
            ok=False, name="WebFetch", error=f"Error fetching '{url}': {exc}", metadata=metadata
        )
    finally:
        if owned_client:
            owned_client.close()
        if on_exit:
            on_exit(activity_id)


handle = handle_web_fetch_tool
