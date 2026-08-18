"""Content Sanitization, HTML-to-Markdown Parsing, Metadata Extraction & Prompt Injection Defense."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# Zero-width / invisible control characters used in prompt injection attacks
INVISIBLE_CHARS_PATTERN = re.compile(r"[\u200B-\u200D\uFEFF\u202A-\u202E\u2060\u180E\u00AD]+")

# Potential LLM delimiter & role hijacking patterns
PROMPT_INJECTION_DELIMITERS = [
    re.compile(r"<\s*\|\s*im_start\s*\|[^>]*>", re.IGNORECASE),
    re.compile(r"<\s*\|\s*im_end\s*\|[^>]*>", re.IGNORECASE),
    re.compile(r"\[\s*(?:system|assistant|developer|instruction)\s*\]", re.IGNORECASE),
    re.compile(r"```(?:system|instruction|prompt)\s*\n[\s\S]*?\n```", re.IGNORECASE),
]

# Common injection attack phrases to sanitize/defang in fetched web content
INJECTION_KEYWORDS_PATTERN = re.compile(
    r"(?i)\b(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"you\s+are\s+now\s+in\s+developer\s+mode|"
    r"override\s+system\s+prompt)\b"
)

# Tags to completely drop with all their inner contents
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
    "header",
    "aside",
}

# Tags that represent block boundaries in Markdown
BLOCK_TAGS = {
    "p",
    "div",
    "article",
    "section",
    "main",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "hr",
    "br",
}


@dataclass
class ExtractedWebPage:
    """Sanitized extracted web page content with metadata."""

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
    """Clean HTML to semantic Markdown converter with tag filtering and security sanitization."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.drop_stack: list[str] = []
        self.tag_stack: list[str] = []
        self.output_chunks: list[str] = []
        self.metadata: dict[str, str] = {}
        self.in_title = False
        self.title_text: list[str] = []
        self.in_pre = False
        self.in_code = False
        self.current_link_url: str | None = None
        self.current_link_text: list[str] = []
        self.list_depth = 0
        self.list_counters: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        # Check for hidden styles (display: none, visibility: hidden, aria-hidden="true")
        style = attr_dict.get("style", "").lower()
        aria_hidden = attr_dict.get("aria-hidden", "").lower()
        hidden_attr = "hidden" in attr_dict
        is_hidden = (
            "display:none" in style.replace(" ", "")
            or "visibility:hidden" in style.replace(" ", "")
            or aria_hidden == "true"
            or hidden_attr
        )

        if tag_lower in DROP_TAGS or is_hidden:
            self.drop_stack.append(tag_lower)
            return

        if self.drop_stack:
            return

        self.tag_stack.append(tag_lower)

        # Metadata extraction
        if tag_lower == "title":
            self.in_title = True
        elif tag_lower == "meta":
            name = attr_dict.get("name") or attr_dict.get("property") or ""
            content = attr_dict.get("content") or ""
            if name and content:
                self.metadata[name.lower()] = content
        elif tag_lower == "link" and attr_dict.get("rel") == "canonical":
            href = attr_dict.get("href")
            if href:
                self.metadata["canonical"] = href

        # Heading tags
        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag_lower[1])
            self._ensure_newline(2)
            self.output_chunks.append("#" * level + " ")

        elif tag_lower == "p":
            self._ensure_newline(2)

        elif tag_lower == "br":
            self.output_chunks.append("\n")

        elif tag_lower == "hr":
            self._ensure_newline(2)
            self.output_chunks.append("---\n\n")

        elif tag_lower == "blockquote":
            self._ensure_newline(2)
            self.output_chunks.append("> ")

        elif tag_lower == "pre":
            self._ensure_newline(2)
            self.in_pre = True
            self.output_chunks.append("```\n")

        elif tag_lower == "code":
            if not self.in_pre:
                self.in_code = True
                self.output_chunks.append("`")

        elif tag_lower in ("ul", "ol"):
            self._ensure_newline(1)
            self.list_depth += 1
            if tag_lower == "ol":
                self.list_counters.append(1)
            else:
                self.list_counters.append(0)

        elif tag_lower == "li":
            self._ensure_newline(1)
            indent = "  " * max(0, self.list_depth - 1)
            if self.list_counters and self.list_counters[-1] > 0:
                count = self.list_counters[-1]
                self.output_chunks.append(f"{indent}{count}. ")
                self.list_counters[-1] += 1
            else:
                self.output_chunks.append(f"{indent}- ")

        elif tag_lower == "a":
            href = attr_dict.get("href")
            if href and not href.startswith("javascript:"):
                self.current_link_url = href
                self.current_link_text = []

        elif tag_lower in ("b", "strong"):
            self.output_chunks.append("**")

        elif tag_lower in ("i", "em"):
            self.output_chunks.append("*")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()

        if self.drop_stack:
            if self.drop_stack[-1] == tag_lower:
                self.drop_stack.pop()
            return

        if self.tag_stack and self.tag_stack[-1] == tag_lower:
            self.tag_stack.pop()

        if tag_lower == "title":
            self.in_title = False
        elif tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"):
            self._ensure_newline(2)
        elif tag_lower == "pre":
            self.in_pre = False
            self._ensure_newline(1)
            self.output_chunks.append("```\n\n")
        elif tag_lower == "code":
            if not self.in_pre and self.in_code:
                self.in_code = False
                self.output_chunks.append("`")
        elif tag_lower in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
            if self.list_counters:
                self.list_counters.pop()
            self._ensure_newline(2)
        elif tag_lower == "li":
            self._ensure_newline(1)
        elif tag_lower == "a":
            if self.current_link_url is not None:
                link_text = "".join(self.current_link_text).strip()
                if link_text:
                    self.output_chunks.append(f"[{link_text}]({self.current_link_url})")
                else:
                    self.output_chunks.append(self.current_link_url)
                self.current_link_url = None
                self.current_link_text = []
        elif tag_lower in ("b", "strong"):
            self.output_chunks.append("**")
        elif tag_lower in ("i", "em"):
            self.output_chunks.append("*")

    def handle_data(self, data: str) -> None:
        if self.drop_stack:
            return

        if self.in_title:
            self.title_text.append(data)
            return

        if self.current_link_url is not None:
            self.current_link_text.append(data)
            return

        self.output_chunks.append(data)

    def handle_comment(self, data: str) -> None:
        # Intentionally drop all HTML comments to eliminate prompt injection vectors
        pass

    def _ensure_newline(self, count: int = 1) -> None:
        if not self.output_chunks:
            return
        last = "".join(self.output_chunks[-3:])
        trailing_newlines = len(last) - len(last.rstrip("\n"))
        needed = max(0, count - trailing_newlines)
        if needed > 0:
            self.output_chunks.append("\n" * needed)

    def get_result(self) -> tuple[str, dict[str, str]]:
        raw = "".join(self.output_chunks)
        title = "".join(self.title_text).strip()
        if title:
            self.metadata["title"] = title
        return raw, self.metadata


def sanitize_prompt_injection(text: str) -> str:
    """Sanitize text to defang prompt injection vectors and remove invisible chars."""
    if not text:
        return text

    # 1. Remove invisible / zero-width characters
    sanitized = INVISIBLE_CHARS_PATTERN.sub("", text)

    # 2. Defang role hijack delimiters
    for pattern in PROMPT_INJECTION_DELIMITERS:
        sanitized = pattern.sub(lambda m: f"({m.group(0).strip('[]<>|`')})", sanitized)

    # 3. Defang injection keywords by wrapping in quotes/neutralizing
    def _defang(match: re.Match) -> str:
        return f"[sanitized prompt injection pattern: {match.group(0)}]"

    sanitized = INJECTION_KEYWORDS_PATTERN.sub(_defang, sanitized)

    return sanitized


def clean_markdown_whitespace(text: str) -> str:
    """Clean redundant blank lines and spaces while preserving markdown formatting."""
    # Collapse multiple consecutive blank lines to max 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace on each line
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def slice_payload(text: str, max_chars: int = 30_000) -> tuple[str, bool]:
    """Token-aware payload slicing: truncate cleanly at paragraph/newline boundaries."""
    if len(text) <= max_chars:
        return text, False

    target_slice = text[:max_chars]
    # Try to find a paragraph break near the end
    last_break = target_slice.rfind("\n\n")
    if last_break >= max_chars * 0.75:
        sliced = target_slice[:last_break].strip()
    else:
        # Try finding a single newline
        last_nl = target_slice.rfind("\n")
        if last_nl >= max_chars * 0.85:
            sliced = target_slice[:last_nl].strip()
        else:
            sliced = target_slice.strip()

    footer = f"\n\n[Content truncated: {len(sliced):,} of {len(text):,} characters displayed]"
    return sliced + footer, True


def extract_and_sanitize_html(
    html_content: str, max_chars: int = 30_000, base_url: str = ""
) -> ExtractedWebPage:
    """Parse HTML, extract metadata, convert to clean Markdown, and sanitize against injection."""
    parser = _HTMLToMarkdownParser()
    try:
        parser.feed(html_content)
        parser.close()
        raw_markdown, meta = parser.get_result()
    except Exception:
        # Fallback regex strip
        raw_text = re.sub(r"<[^>]+>", " ", html_content)
        raw_markdown = html.unescape(raw_text)
        meta = {}

    title = meta.get("title") or meta.get("og:title") or ""
    description = meta.get("description") or meta.get("og:description") or ""
    author = meta.get("author") or meta.get("article:author") or ""
    canonical = meta.get("canonical") or meta.get("og:url") or base_url

    # Apply prompt injection defense
    sanitized_md = sanitize_prompt_injection(raw_markdown)
    cleaned_md = clean_markdown_whitespace(sanitized_md)

    sliced_md, truncated = slice_payload(cleaned_md, max_chars=max_chars)

    return ExtractedWebPage(
        title=title,
        description=description,
        author=author,
        canonical_url=canonical,
        markdown=sliced_md,
        raw_text=cleaned_md,
        total_chars=len(cleaned_md),
        truncated=truncated,
        metadata=meta,
    )
