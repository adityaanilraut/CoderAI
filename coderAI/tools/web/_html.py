"""HTML/PDF content extraction: text conversion, readability, metadata."""

import html as html_lib
import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# HTML2Text — shared instance for HTML→Markdown conversion
# ═══════════════════════════════════════════════════════════════════════════

_h2t = None


def _get_h2t():
    global _h2t
    if _h2t is None:
        import html2text

        _h2t = html2text.HTML2Text()
        _h2t.body_width = 0  # don't wrap
        _h2t.ignore_links = False
        _h2t.ignore_images = True
        _h2t.ignore_emphasis = False
        _h2t.ignore_tables = False
        _h2t.protect_links = False
        _h2t.unicode_snob = True
        _h2t.skip_internal_links = True
        _h2t.single_line_break = False
        _h2t.mark_code = True
        _h2t.wrap_links = False
    return _h2t


# ═══════════════════════════════════════════════════════════════════════════
# Precompiled Regex Patterns
# ═══════════════════════════════════════════════════════════════════════════

_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_MULTI_NL = re.compile(r"\n{3,}")
_MULTI_SP = re.compile(r"[ \t]{2,}")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_STRIP_BLOCK_RE = re.compile(
    r"<(?:script|style|noscript|svg|head|nav|footer|header)[^>]*>.*?</(?:script|style|noscript|svg|head|nav|footer|header)>",
    re.DOTALL | re.IGNORECASE,
)

_META_OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:(\w+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_NAME_RE = re.compile(
    r'<meta[^>]+name=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_TWITTER_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:(\w+)["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.DOTALL)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.DOTALL
)

# Readability extraction — regex-based replacement for HTMLParser scorer
_READABILITY_ARTICLE_RE = re.compile(
    r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>", re.DOTALL | re.IGNORECASE
)
_READABILITY_SECTION_RE = re.compile(
    r"<(?:section|div)[^>]*>(.*?)</(?:section|div)>", re.DOTALL | re.IGNORECASE
)
_READABILITY_NAV_FOOTER_RE = re.compile(
    r"<(?:nav|footer|header|aside)[^>]*>.*?</(?:nav|footer|header|aside)>",
    re.DOTALL | re.IGNORECASE,
)
_LINK_DENSITY_RE = re.compile(r"<a[^>]*>.*?</a>", re.DOTALL | re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════
# HTML Utilities
# ═══════════════════════════════════════════════════════════════════════════


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _looks_like_html(content_type: str, raw: str) -> bool:
    return "html" in (content_type or "").lower() or raw.lstrip().startswith("<")


def _html_to_text(raw_html: str, fmt: str) -> str:
    """Convert HTML to the requested format using html2text or plain-text stripping."""
    if fmt == "html":
        return raw_html
    if not _looks_like_html("text/html", raw_html):
        return raw_html
    if fmt == "text":
        text = _STRIP_BLOCK_RE.sub("", raw_html)
        text = _COMMENT_RE.sub("", text)
        text = _TAG_RE.sub(" ", text)
        text = html_lib.unescape(text)
        text = _MULTI_SP.sub(" ", text)
        text = _MULTI_NL.sub("\n\n", text)
        return text.strip()
    # markdown (default)
    return str(_get_h2t().handle(raw_html).strip())


# ═══════════════════════════════════════════════════════════════════════════
# Content Extraction — Readability (regex-based, ~10x faster than HTMLParser)
# ═══════════════════════════════════════════════════════════════════════════


def _extract_main_content(html: str) -> str:
    """Extract the main content block from a web page using regex heuristics."""
    # Strip non-content blocks
    html = _STRIP_BLOCK_RE.sub("", html)
    html = _COMMENT_RE.sub("", html)

    # Try <article> or <main> first — highest semantic weight
    m = _READABILITY_ARTICLE_RE.search(html)
    if m:
        return m.group(1)

    # Split into large blocks, score by text density
    blocks = _READABILITY_SECTION_RE.findall(html)
    if not blocks:
        return html

    best_text = ""
    best_score = 0.0

    for block in blocks:
        text = _TAG_RE.sub(" ", block)
        text = html_lib.unescape(text)
        text = _MULTI_SP.sub(" ", text).strip()
        if len(text) < 50:
            continue

        link_text = " ".join(_LINK_DENSITY_RE.findall(block))
        link_text = _TAG_RE.sub("", link_text).strip()

        text_len = len(text)
        link_ratio = len(link_text) / text_len if text_len > 0 else 1.0

        # Score: prefer long text blocks with low link density
        score = min(text_len / 80, 10.0)
        if link_ratio > 0.5:
            score *= 0.2
        elif link_ratio > 0.3:
            score *= 0.5

        if score > best_score:
            best_score = score
            best_text = text

    return best_text if best_text else html


# ═══════════════════════════════════════════════════════════════════════════
# Metadata Extraction
# ═══════════════════════════════════════════════════════════════════════════


def _extract_metadata(html: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    m = _TITLE_RE.search(html)
    if m:
        meta["title"] = _strip_tags(m.group(1))
    for m in _META_OG_RE.finditer(html):
        meta[f"og:{m.group(1)}"] = m.group(2)
    for m in _META_TWITTER_RE.finditer(html):
        meta[f"twitter:{m.group(1)}"] = m.group(2)
    for m in _META_NAME_RE.finditer(html):
        name = m.group(1).lower()
        if name not in meta:
            meta[name] = m.group(2)
    for m in _JSONLD_RE.findall(html):
        try:
            ld = json.loads(m)
            if isinstance(ld, dict):
                meta["jsonld_type"] = ld.get("@type", "")
                if "name" in ld:
                    meta.setdefault("title", str(ld["name"]))
                if "description" in ld:
                    meta.setdefault("description", str(ld["description"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return meta


# ═══════════════════════════════════════════════════════════════════════════
# PDF Text Extraction
# ═══════════════════════════════════════════════════════════════════════════


def _extract_pdf_text(content: bytes) -> Optional[str]:
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        pages: List[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages) if pages else None
    except ImportError:
        logger.debug("pypdf not installed; install with: pip install pypdf")
        return None
    except Exception as e:
        # Best-effort: a None return makes the caller fall back to raw-text extraction.
        logger.debug(f"PDF extraction failed: {e}", exc_info=True)
        return None
