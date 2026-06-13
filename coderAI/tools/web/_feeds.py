"""RSS/Atom feed parsing and sitemap discovery helpers."""

import logging
from typing import Any, Dict, List, Optional

# Network fetches go through the package namespace so tests can patch
# coderAI.tools.web._safe_request_cf as a single point, exactly as they
# could when everything lived in one module.
import coderAI.tools.web as _web

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Feed Parsing (RSS / Atom)
# ═══════════════════════════════════════════════════════════════════════════


def _parse_feed(raw: str, max_entries: int) -> List[Dict[str, Any]]:
    from xml.etree import ElementTree as ET

    entries: List[Dict[str, Any]] = []
    try:
        start = raw.find("<")
        if start > 0:
            raw = raw[start:]
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        logger.debug(f"XML parse error: {e}")
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc": "http://purl.org/dc/elements/1.1/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }

    for item in root.iter("item"):
        if len(entries) >= max_entries:
            break
        entries.append(_parse_rss_item(item, ns))

    for entry_elem in root.iter("{http://www.w3.org/2005/Atom}entry"):
        if len(entries) >= max_entries:
            break
        entries.append(_parse_atom_entry(entry_elem, ns))

    return entries


def _parse_rss_item(item, ns: Dict[str, str]) -> Dict[str, Any]:
    def _text(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    link = _text("link")
    if not link:
        guid = item.find("guid")
        if guid is not None and guid.text:
            link = guid.text.strip()

    return {
        "title": _text("title"),
        "link": link,
        "published": _text("pubDate") or _text("{http://purl.org/dc/elements/1.1/}date"),
        "summary": _text("description")
        or _text("{http://purl.org/rss/1.0/modules/content/}encoded"),
        "author": _text("author") or _text("{http://purl.org/dc/elements/1.1/}creator"),
    }


def _parse_atom_entry(entry, ns: Dict[str, str]) -> Dict[str, Any]:
    def _text(tag: str) -> str:
        el = entry.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    link = ""
    for link_el in entry.findall("{http://www.w3.org/2005/Atom}link"):
        rel = link_el.get("rel", "alternate")
        href = link_el.get("href", "")
        if rel == "alternate" and href:
            link = href
            break
    if not link:
        for link_el in entry.findall("{http://www.w3.org/2005/Atom}link"):
            href = link_el.get("href", "")
            if href:
                link = href
                break

    return {
        "title": _text("{http://www.w3.org/2005/Atom}title"),
        "link": link,
        "published": _text("{http://www.w3.org/2005/Atom}updated")
        or _text("{http://www.w3.org/2005/Atom}published"),
        "summary": _text("{http://www.w3.org/2005/Atom}summary")
        or _text("{http://www.w3.org/2005/Atom}content"),
        "author": "",
    }


def _extract_feed_metadata(raw: str) -> Dict[str, str]:
    from xml.etree import ElementTree as ET

    try:
        start = raw.find("<")
        if start > 0:
            raw = raw[start:]
        root = ET.fromstring(raw)

        channel = root.find("channel")
        if channel is not None:
            title_el = channel.find("title")
            desc_el = channel.find("description")
            return {
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "description": (desc_el.text or "").strip() if desc_el is not None else "",
            }

        title_el = root.find("{http://www.w3.org/2005/Atom}title")
        return {
            "title": (title_el.text or "").strip() if title_el is not None else "",
        }
    except Exception:
        # Feed metadata is decorative; entries are parsed separately, so a
        # malformed channel header should not fail the whole feed read.
        logger.debug("feed metadata parse failed", exc_info=True)
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# Sitemap Discovery
# ═══════════════════════════════════════════════════════════════════════════


async def _discover_sitemap_from_robots(robots_url: str) -> Optional[str]:
    try:
        resp = await _web._safe_request_cf("GET", robots_url, timeout_s=10.0)
        if resp and resp["status"] == 200:
            for line in resp["text"].splitlines():
                line_lower = line.strip().lower()
                if line_lower.startswith("sitemap:"):
                    return str(line.strip().split(":", 1)[1].strip())
    except Exception as e:
        # robots.txt is only one discovery source; callers try common paths next.
        logger.debug(f"robots.txt fetch failed: {e}", exc_info=True)
    return None


async def _url_exists(url: str) -> bool:
    try:
        resp = await _web._safe_request_cf("HEAD", url, timeout_s=10.0)
        return resp is not None and resp["status"] == 200
    except Exception:
        # Existence probe: any failure (network, SSRF block) means "not found".
        logger.debug(f"HEAD probe failed for {url}", exc_info=True)
        return False


async def _fetch_sitemap_urls(sitemap_url: str, max_urls: int) -> List[str]:
    try:
        resp = await _web._safe_request_cf("GET", sitemap_url, timeout_s=20.0)
        if not resp or resp["status"] != 200:
            return []
    except Exception as e:
        # One unreachable sitemap shouldn't fail discovery; return no URLs.
        logger.debug(f"Sitemap fetch failed: {e}", exc_info=True)
        return []
    return _parse_sitemap(resp["text"], max_urls)


def _parse_sitemap(xml_text: str, max_urls: int) -> List[str]:
    from xml.etree import ElementTree as ET

    try:
        start = xml_text.find("<")
        if start > 0:
            xml_text = xml_text[start:]
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    urls: List[str] = []
    ns_url = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

    for url_elem in root.iter(f"{ns_url}url"):
        if len(urls) >= max_urls:
            break
        loc = url_elem.find(f"{ns_url}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())

    if not urls:
        for sm_elem in root.iter(f"{ns_url}sitemap"):
            loc = sm_elem.find(f"{ns_url}loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    if not urls:
        for url_elem in root.iter("url"):
            if len(urls) >= max_urls:
                break
            loc = url_elem.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    if not urls:
        for sm_elem in root.iter("sitemap"):
            loc = sm_elem.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())

    return urls
