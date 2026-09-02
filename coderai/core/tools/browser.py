"""Headless browser automation and DOM extraction engine for CoderAI subagents."""

from __future__ import annotations

import html
import html.parser
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from coderai.core.tools.types import ToolResult


@dataclass
class InteractiveElement:
    """Represents a focusable, clickable, or input-capable DOM node."""

    ref_id: int
    tag: str
    text: str
    role: str = ""
    selector: str = ""
    href: str | None = None
    input_type: str | None = None
    value: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ref": f"[#{self.ref_id}]",
            "ref_id": self.ref_id,
            "tag": self.tag,
            "text": self.text,
            "selector": self.selector,
        }
        if self.role:
            d["role"] = self.role
        if self.href:
            d["href"] = self.href
        if self.input_type:
            d["type"] = self.input_type
        if self.value:
            d["value"] = self.value
        return d


@dataclass
class BrowserState:
    """Current state of the headless browser session."""

    url: str = ""
    title: str = ""
    content_html: str = ""
    text_content: str = ""
    elements: list[InteractiveElement] = field(default_factory=list)
    scroll_position: dict[str, int] = field(default_factory=lambda: {"x": 0, "y": 0})
    history: list[str] = field(default_factory=list)

    def format_summary(self, max_text_chars: int = 2000) -> str:
        lines: list[str] = []
        lines.append(f"URL: {self.url or 'about:blank'}")
        lines.append(f"Title: {self.title or '(Untitled)'}")
        lines.append("")

        if self.elements:
            lines.append("Interactive Elements:")
            for elem in self.elements[:50]:
                tag_label = elem.tag.upper()
                text_preview = elem.text.strip()[:40] if elem.text else ""
                extra = []
                if elem.href:
                    extra.append(f"href={elem.href}")
                if elem.input_type:
                    extra.append(f"type={elem.input_type}")
                if elem.value:
                    extra.append(f"value={elem.value}")
                extra_str = f" ({', '.join(extra)})" if extra else ""
                lines.append(f"  [#{elem.ref_id}] <{tag_label}> {text_preview}{extra_str}")
            if len(self.elements) > 50:
                lines.append(f"  ... ({len(self.elements) - 50} more elements)")
            lines.append("")

        if self.text_content:
            clean_text = self.text_content.strip()
            if len(clean_text) > max_text_chars:
                clean_text = clean_text[:max_text_chars] + "... [truncated]"
            lines.append("Page Content:")
            lines.append(clean_text)

        return "\n".join(lines)


class DOMExtractor(html.parser.HTMLParser):
    """Parses HTML into text content and indexes interactive elements with unique ref IDs."""

    INTERACTIVE_TAGS = {"a", "button", "input", "textarea", "select", "option", "summary"}

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self._in_title = False
        self._in_script_or_style = False
        self.text_parts: list[str] = []
        self.elements: list[InteractiveElement] = []
        self._elem_counter = 0
        self._current_tag: str | None = None
        self._current_attrs: dict[str, str] = {}
        self._current_elem_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "noscript"):
            self._in_script_or_style = True
            return

        if tag_lower == "title":
            self._in_title = True
            return

        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag_lower in self.INTERACTIVE_TAGS or attr_dict.get("role") in (
            "button",
            "link",
            "checkbox",
            "textbox",
        ):
            self._current_tag = tag_lower
            self._current_attrs = attr_dict
            self._current_elem_text = []

            # Direct self-closing/void input tags
            if tag_lower == "input":
                self._elem_counter += 1
                elem = InteractiveElement(
                    ref_id=self._elem_counter,
                    tag=tag_lower,
                    text=attr_dict.get("placeholder") or attr_dict.get("name") or "",
                    role=attr_dict.get("role", "input"),
                    selector=f"input[name='{attr_dict.get('name')}']"
                    if attr_dict.get("name")
                    else f"input#{attr_dict.get('id')}"
                    if attr_dict.get("id")
                    else f"input:nth-of-type({self._elem_counter})",
                    input_type=attr_dict.get("type", "text"),
                    value=attr_dict.get("value"),
                    attributes=attr_dict,
                )
                self.elements.append(elem)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("script", "style", "noscript"):
            self._in_script_or_style = False
            return

        if tag_lower == "title":
            self._in_title = False
            return

        if self._current_tag == tag_lower and tag_lower != "input":
            self._elem_counter += 1
            inner_text = " ".join(self._current_elem_text).strip()
            selector = ""
            elem_id = self._current_attrs.get("id")
            if elem_id:
                selector = f"#{elem_id}"
            else:
                elem_name = self._current_attrs.get("name")
                if elem_name:
                    selector = f"{tag_lower}[name='{elem_name}']"
                else:
                    selector = f"{tag_lower}:nth-of-type({self._elem_counter})"

            elem = InteractiveElement(
                ref_id=self._elem_counter,
                tag=tag_lower,
                text=inner_text,
                role=self._current_attrs.get("role", tag_lower),
                selector=selector,
                href=self._current_attrs.get("href"),
                value=self._current_attrs.get("value"),
                attributes=self._current_attrs,
            )
            self.elements.append(elem)
            self._current_tag = None
            self._current_attrs = {}
            self._current_elem_text = []

    def handle_data(self, data: str) -> None:
        if self._in_script_or_style:
            return
        if self._in_title:
            self.title += data
            return

        text = data.strip()
        if text:
            self.text_parts.append(text)
            if self._current_tag is not None:
                self._current_elem_text.append(text)


class HeadlessBrowserDriver:
    """Manages browser session state, navigation, and DOM interactions."""

    def __init__(self) -> None:
        self.state = BrowserState()

    def navigate(self, url: str, html_override: str | None = None) -> BrowserState:
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://", "file://", "about:")):
            clean_url = f"https://{clean_url}"

        html_content = html_override
        if html_content is None:
            if clean_url.startswith("about:"):
                html_content = "<html><head><title>Blank</title></head><body><h1>About Blank</h1></body></html>"
            else:
                try:
                    req = urllib.request.Request(
                        clean_url,
                        headers={"User-Agent": "CoderAI-HeadlessBrowser/1.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                except Exception as exc:
                    html_content = f"<html><head><title>Error</title></head><body><h1>Failed to load {html.escape(clean_url)}</h1><p>{html.escape(str(exc))}</p></body></html>"

        extractor = DOMExtractor()
        try:
            extractor.feed(html_content)
        except Exception:
            pass

        self.state.url = clean_url
        self.state.title = extractor.title.strip() or clean_url
        self.state.content_html = html_content
        self.state.text_content = " ".join(extractor.text_parts)
        self.state.elements = extractor.elements
        self.state.history.append(clean_url)
        return self.state

    def _resolve_element(self, ref_or_selector: int | str) -> InteractiveElement | None:
        if isinstance(ref_or_selector, int):
            return next((e for e in self.state.elements if e.ref_id == ref_or_selector), None)

        val_str = str(ref_or_selector).strip()
        match = re.match(r"^\[?#?(\d+)\]?$", val_str)
        if match:
            target_id = int(match.group(1))
            return next((e for e in self.state.elements if e.ref_id == target_id), None)

        # Match by selector or text
        for elem in self.state.elements:
            if elem.selector == val_str or elem.text.strip().lower() == val_str.lower():
                return elem
        return None

    def click(self, ref_or_selector: int | str) -> tuple[bool, str, BrowserState]:
        elem = self._resolve_element(ref_or_selector)
        if not elem:
            return False, f"Element '{ref_or_selector}' not found in DOM.", self.state

        if elem.href:
            target_url = urllib.parse.urljoin(self.state.url, elem.href)
            self.navigate(target_url)
            return (
                True,
                f"Clicked [#{elem.ref_id}] <{elem.tag}> and navigated to {target_url}",
                self.state,
            )

        return True, f"Clicked [#{elem.ref_id}] <{elem.tag}> '{elem.text}'", self.state

    def type(
        self,
        ref_or_selector: int | str,
        text: str,
        clear_first: bool = True,
    ) -> tuple[bool, str, BrowserState]:
        elem = self._resolve_element(ref_or_selector)
        if not elem:
            return False, f"Input element '{ref_or_selector}' not found in DOM.", self.state

        if clear_first or elem.value is None:
            elem.value = text
        else:
            elem.value = str(elem.value) + text

        return True, f"Typed text into [#{elem.ref_id}] <{elem.tag}>: '{text}'", self.state

    def snapshot(self, extract_dom: bool = True) -> BrowserState:
        return self.state

    def close(self) -> None:
        self.state = BrowserState()


_global_browser = HeadlessBrowserDriver()


def get_browser_driver() -> HeadlessBrowserDriver:
    return _global_browser


# ==============================================================================
# Tool Handlers
# ==============================================================================


def handle_browser_navigate_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Navigate headless browser to a URL and extract title and interactive elements."""
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult(ok=False, name="browser_navigate", error="Parameter `url` is required.")

    html_override = args.get("html_override")
    driver = get_browser_driver()
    state = driver.navigate(url, html_override=html_override)

    return ToolResult(
        ok=True,
        name="browser_navigate",
        output=state.format_summary(),
        metadata={
            "url": state.url,
            "title": state.title,
            "element_count": len(state.elements),
            "elements": [e.to_dict() for e in state.elements],
        },
    )


def handle_browser_click_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Click an indexed element [#{ref_id}] or selector in the active browser page."""
    elem_ref = args.get("element_ref")
    if elem_ref is None:
        return ToolResult(
            ok=False,
            name="browser_click",
            error="Parameter `element_ref` is required (e.g. 1 or '#1').",
        )

    driver = get_browser_driver()
    ok, msg, state = driver.click(elem_ref)
    if not ok:
        return ToolResult(ok=False, name="browser_click", error=msg)

    return ToolResult(
        ok=True,
        name="browser_click",
        output=f"{msg}\n\n{state.format_summary()}",
        metadata={"url": state.url, "title": state.title},
    )


def handle_browser_type_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Type text into an input or textarea element on the active browser page."""
    elem_ref = args.get("element_ref")
    text = args.get("text")
    if elem_ref is None or text is None:
        return ToolResult(
            ok=False,
            name="browser_type",
            error="Parameters `element_ref` and `text` are required.",
        )

    clear_first = bool(args.get("clear_first", True))
    driver = get_browser_driver()
    ok, msg, state = driver.type(elem_ref, str(text), clear_first=clear_first)
    if not ok:
        return ToolResult(ok=False, name="browser_type", error=msg)

    return ToolResult(
        ok=True,
        name="browser_type",
        output=f"{msg}\n\n{state.format_summary()}",
        metadata={"url": state.url, "title": state.title},
    )


def handle_browser_snapshot_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Capture current DOM tree snapshot, scroll position, and text/element catalog."""
    driver = get_browser_driver()
    state = driver.snapshot(extract_dom=bool(args.get("extract_dom", True)))

    return ToolResult(
        ok=True,
        name="browser_snapshot",
        output=state.format_summary(),
        metadata={
            "url": state.url,
            "title": state.title,
            "element_count": len(state.elements),
            "elements": [e.to_dict() for e in state.elements],
        },
    )


def handle_browser_close_tool(args: dict[str, Any], context: Any) -> ToolResult:
    """Close active browser session and reset state."""
    driver = get_browser_driver()
    driver.close()
    return ToolResult(ok=True, name="browser_close", output="Browser session closed.")
