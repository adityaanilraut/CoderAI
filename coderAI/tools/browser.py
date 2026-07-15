"""Browser automation tools using Playwright (cross-platform).

These tools provide full browser control: navigate, inspect the accessibility
snapshot, click/type by element reference, extract page content, take
screenshots, evaluate JavaScript, and close the browser.

Requires ``playwright`` (install with ``playwright install chromium`` after
``pip install coderAI[browser]``). Tools gracefully degrade with a clear
error message when the dependency is missing.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from coderAI.core.provenance import Provenance
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.fsperms import atomic_write_bytes
from coderAI.tools.base import Tool
from coderAI.tools.filesystem._guards import (
    ProjectPathError,
    _reject_symlink_leaf,
    resolve_under_project,
)
from coderAI.tools.web import _is_ip_public

logger = logging.getLogger(__name__)

# The Playwright dependency is declared per tool via ``requires_package``
# (Phase 4.2); Agent._create_tool_registry drops tools whose package is missing.

# ---------------------------------------------------------------------------
# Playwright availability check
# ---------------------------------------------------------------------------

_playwright_available: Optional[bool] = None


def _check_playwright() -> Optional[Dict[str, Any]]:
    """Return an error dict if Playwright is not installed, else None."""
    global _playwright_available
    if _playwright_available is None:
        try:
            import playwright  # noqa: F401

            _playwright_available = True
        except ImportError:
            _playwright_available = False
    if not _playwright_available:
        return {
            "success": False,
            "error": (
                "Playwright is not installed. Install with: "
                "pip install coderAI[browser] && playwright install chromium"
            ),
        }
    return None


# ---------------------------------------------------------------------------
# SSRF guard — reuse the same public-IP check the web tools use
# (``_is_ip_public``, imported above).
# ---------------------------------------------------------------------------


async def _resolve_host_addrs(host: str) -> List[str]:
    """Resolve ``host`` to its IP addresses without blocking the event loop."""
    import socket

    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos if info[0] in (socket.AF_INET, socket.AF_INET6)]


async def _validate_navigation_url(url: str) -> Optional[Dict[str, Any]]:
    """Check a URL for SSRF before allowing browser navigation (6.2).

    Rejects non-http(s) schemes and literal private IPs, then — for a hostname —
    **resolves it and requires every resolved address to be public**. This is the
    browser mirror of the web layer's ``_SSRFResolver``: without it a hostname
    that resolves to link-local/loopback/RFC1918 (including a DNS-rebinding
    record) would sail past the literal-IP check. The post-navigation ``page.url``
    re-check in :meth:`BrowserSession.navigate` closes the redirect path.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"success": False, "error": f"Unsupported URL scheme: {parsed.scheme}"}

    host = parsed.hostname
    if not host:
        return {"success": False, "error": "SSRF guard: URL has no host"}

    # Literal IP: validate directly, no DNS lookup needed.
    try:
        ip = ipaddress.ip_address(host)
        if not _is_ip_public(str(ip)):
            return {
                "success": False,
                "error": f"SSRF guard: blocked navigation to {host}",
            }
        return None
    except ValueError:
        pass  # a hostname → resolve and pin below

    addrs = await _resolve_host_addrs(host)
    if not addrs:
        return {"success": False, "error": f"SSRF guard: could not resolve {host}"}
    for addr in addrs:
        if not _is_ip_public(addr):
            return {
                "success": False,
                "error": f"SSRF guard: '{host}' resolves to non-public address {addr}",
            }
    return None


def _get_allowed_domains() -> Optional[List[str]]:
    """Read the comma-separated allowed-domains list from config."""
    try:
        from coderAI.core.services import get_services

        cfg = get_services().config
        raw = cfg.browser_allowed_domains
        if raw:
            return [d.strip().lower() for d in raw.split(",") if d.strip()]
    except Exception:
        # Unreadable config is treated like an unset allowlist (no domain
        # restriction) — same default as a fresh install.
        logger.debug("browser_allowed_domains config unavailable", exc_info=True)
    return None


def _check_domain_allowlist(url: str) -> Optional[Dict[str, Any]]:
    """If allowed domains are configured, reject URLs outside that list."""
    allowed = _get_allowed_domains()
    if not allowed:
        return None
    hostname = (urlparse(url).hostname or "").lower()
    for domain in allowed:
        domain = domain.lstrip(".")
        if hostname == domain or hostname.endswith("." + domain):
            return None
    return {
        "success": False,
        "error": f"Domain '{hostname}' is not in the allowed list: {', '.join(allowed)}",
    }


async def _validate_browser_request(url: str) -> Optional[Dict[str, Any]]:
    """Validate one Playwright request against browser network policy."""
    try:
        if err := await _validate_navigation_url(url):
            return err
        return _check_domain_allowlist(url)
    except Exception as exc:
        logger.warning("Browser request validation failed closed for %s: %s", url, exc)
        return {
            "success": False,
            "error": f"Browser request blocked: policy validation failed for {url}: {exc}",
        }


def _active_project_root() -> Path:
    try:
        from coderAI.core.services import get_services

        return Path(getattr(get_services().config, "project_root", ".") or ".").resolve()
    except Exception:
        logger.debug("project_root config unavailable, using cwd", exc_info=True)
        return Path.cwd().resolve()


def _guard_screenshot_path(path: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Resolve and validate a screenshot destination using filesystem guards."""
    try:
        target = resolve_under_project(
            path,
            operation="save screenshot",
            check_protected=True,
            reject_symlink=True,
        )
    except ProjectPathError as exc:
        return None, exc.as_result()

    # Screenshot output is always project-scoped. Unlike general filesystem
    # tools, CODERAI_ALLOW_OUTSIDE_PROJECT must not widen this browser boundary.
    try:
        target.relative_to(_active_project_root())
    except ValueError:
        return None, {
            "success": False,
            "error": f"Refusing to save screenshot outside project root: {target}",
            "error_code": ToolErrorCode.SCOPE,
        }
    if not target.parent.exists() or not target.parent.is_dir():
        return None, {
            "success": False,
            "error": f"Screenshot destination directory does not exist: {target.parent}",
        }
    if target.exists() and not target.is_file():
        return None, {
            "success": False,
            "error": f"Screenshot destination is not a regular file: {target}",
        }
    return target, None


# ---------------------------------------------------------------------------
# BrowserSession — per-agent Playwright life-cycle
# ---------------------------------------------------------------------------


def _playwright_manager() -> Any:
    """Load the optional Playwright dependency only when browser use starts."""
    from playwright.async_api import async_playwright

    return async_playwright()


class BrowserSession:
    """Lazily initialised Playwright browser (one persistent page per agent)."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._ref_map: Dict[str, Dict[str, str]] = {}
        self._blocked_request_reason: Optional[str] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("Browser is not initialized. Call browser_navigate first.")
        return self._page

    async def _ensure_browser(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return

            self._playwright = await _playwright_manager().start()

            headless = True
            try:
                from coderAI.core.services import get_services

                headless = get_services().config.browser_headless
            except Exception:
                # Config unavailable → keep the safe headless default.
                logger.debug("browser_headless config unavailable, using default", exc_info=True)

            self._browser = await self._playwright.chromium.launch(headless=headless)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                service_workers="block",
            )

            # Context routes cover every page and popup. Install them before the
            # first page exists so no navigation or subresource can race policy.
            await self._context.route("**/*", self._intercept_request)

            # Playwright only gained route_web_socket in newer releases. Use it
            # when present and also inject a constructor block for older versions.
            route_web_socket = getattr(self._context, "route_web_socket", None)
            if route_web_socket is not None:
                await route_web_socket("**/*", self._block_websocket)
            await self._context.add_init_script(
                script="""
                    Object.defineProperty(globalThis, 'WebSocket', {
                      configurable: false,
                      value: class BlockedWebSocket {
                        constructor() {
                          throw new DOMException(
                            'WebSockets are disabled by CoderAI browser policy',
                            'SecurityError'
                          );
                        }
                      }
                    });
                """
            )
            self._page = await self._context.new_page()
            self._initialized = True
            logger.info(
                "BrowserSession[%s]: Chromium launched (headless=%s)",
                self.agent_id,
                headless,
            )

    def _record_blocked_request(self, reason: str) -> None:
        if self._blocked_request_reason is None:
            self._blocked_request_reason = reason
        logger.warning("BrowserSession[%s]: %s", self.agent_id, reason)

    async def _intercept_request(self, route: Any, request: Any) -> None:
        """Fail closed before Playwright dispatches any network request."""
        url = str(request.url)
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            err = await _validate_browser_request(url)
        elif scheme in ("about", "blob", "data"):
            err = None
        else:
            err = {
                "success": False,
                "error": f"Browser request blocked: unsupported URL scheme '{scheme}' for {url}",
            }

        if err is not None:
            self._record_blocked_request(str(err["error"]))
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _block_websocket(self, websocket: Any) -> None:
        url = str(getattr(websocket, "url", "unknown WebSocket"))
        reason = f"Browser request blocked: WebSockets are disabled ({url})"
        self._record_blocked_request(reason)
        await websocket.close(code=1008, reason="Blocked by CoderAI browser policy")

    def _begin_network_action(self) -> None:
        self._blocked_request_reason = None

    def _blocked_request_result(self) -> Optional[Dict[str, Any]]:
        if self._blocked_request_reason is None:
            return None
        return {
            "success": False,
            "error": self._blocked_request_reason,
            "error_code": ToolErrorCode.TOOL_ERROR,
        }

    def _get_timeout(self) -> float:
        try:
            from coderAI.core.services import get_services

            return float(get_services().config.browser_timeout)
        except Exception:
            # Config unavailable → built-in 30s default.
            logger.debug("browser_timeout config unavailable, using default", exc_info=True)
            return 30.0

    async def navigate(self, url: str) -> Dict[str, Any]:
        await self._ensure_browser()
        self._begin_network_action()
        try:
            await self._page.goto(
                url, wait_until="domcontentloaded", timeout=self._get_timeout() * 1000
            )
            current_url = self._page.url
            # Re-validate the *final* URL after any redirects: a public URL can
            # 3xx to (or DNS-rebind toward) an internal-resolving host, which the
            # pre-navigation check on the original URL couldn't catch (6.2). On a
            # block, reset the page so the internal content isn't left loaded.
            if (err := await _validate_navigation_url(current_url)) or (
                err := _check_domain_allowlist(current_url)
            ):
                try:
                    await self._page.goto("about:blank")
                except Exception:
                    logger.debug("failed to blank page after blocked redirect", exc_info=True)
                return err
            if blocked := self._blocked_request_result():
                return blocked
            title = await self._page.title()
            return {
                "success": True,
                "url": current_url,
                "title": title,
                "message": f"Navigated to {current_url}",
            }
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"Navigation failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def snapshot(self) -> str:
        await self._ensure_browser()
        snapshot_text, ref_map = await _build_accessibility_snapshot(self._page)
        self._ref_map = ref_map
        return snapshot_text

    async def click_by_ref(self, ref: str) -> Dict[str, Any]:
        await self._ensure_browser()
        if ref not in self._ref_map:
            return {
                "success": False,
                "error": f"Element ref '{ref}' not found in the current snapshot. "
                f"Call browser_snapshot first to get current refs. "
                f"Available refs: {sorted(self._ref_map.keys())[:20]}...",
            }
        info = self._ref_map[ref]
        role = info["role"]
        name = info.get("name", "")
        self._begin_network_action()
        try:
            locator = self._page.get_by_role(role, name=name, exact=False)  # type: ignore[arg-type]
            count = await locator.count()
            if count == 0:
                locator = self._page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                count = await locator.count()
            if count == 0:
                return {
                    "success": False,
                    "error": f"No element found with role='{role}' name='{name}'. "
                    f"The page may have changed — try browser_snapshot again.",
                }
            if count > 1:
                logger.info(
                    "Multiple matches (%d) for role='%s' name='%s' — clicking first",
                    count,
                    role,
                    name,
                )
            await locator.first.click(timeout=self._get_timeout() * 1000)
            if blocked := self._blocked_request_result():
                return blocked
            return {"success": True, "message": f"Clicked [{ref}] {role} '{name}'"}
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"Click failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def type_by_ref(self, ref: str, text: str, clear: bool = False) -> Dict[str, Any]:
        await self._ensure_browser()
        if ref not in self._ref_map:
            return {
                "success": False,
                "error": f"Element ref '{ref}' not found in the current snapshot. "
                f"Call browser_snapshot first to get current refs.",
            }
        info = self._ref_map[ref]
        role = info["role"]
        name = info.get("name", "")
        self._begin_network_action()
        try:
            locator = self._page.get_by_role(role, name=name, exact=False)  # type: ignore[arg-type]
            count = await locator.count()
            if count == 0:
                locator = self._page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                count = await locator.count()
            if count == 0:
                return {
                    "success": False,
                    "error": f"No element found with role='{role}' name='{name}'. "
                    f"Try browser_snapshot again.",
                }
            target = locator.first
            if clear:
                await target.clear()
            await target.fill(text, timeout=self._get_timeout() * 1000)
            if blocked := self._blocked_request_result():
                return blocked
            return {"success": True, "message": f"Typed '{text}' into [{ref}] {role} '{name}'"}
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"Type failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def select_option_by_ref(self, ref: str, value: str) -> Dict[str, Any]:
        await self._ensure_browser()
        if ref not in self._ref_map:
            return {
                "success": False,
                "error": f"Element ref '{ref}' not found. Call browser_snapshot first.",
            }
        info = self._ref_map[ref]
        role = info["role"]
        name = info.get("name", "")
        self._begin_network_action()
        try:
            locator = self._page.get_by_role(role, name=name, exact=False)  # type: ignore[arg-type]
            count = await locator.count()
            if count == 0:
                locator = self._page.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]
                count = await locator.count()
            if count == 0:
                return {"success": False, "error": f"No select element found for [{ref}]."}
            await locator.first.select_option(value, timeout=self._get_timeout() * 1000)
            if blocked := self._blocked_request_result():
                return blocked
            return {"success": True, "message": f"Selected '{value}' in [{ref}] {role} '{name}'"}
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"Select failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def get_content(self, fmt: str = "markdown") -> str:
        await self._ensure_browser()
        if fmt == "text":
            return str(await self._page.inner_text("body"))
        if fmt == "html":
            return str(await self._page.content())
        try:
            from coderAI.tools.web import _html_to_text
        except ImportError:
            logger.warning("html2text not available, falling back to plain text")
            return str(await self._page.inner_text("body"))
        html = await self._page.content()
        return _html_to_text(html, "markdown")

    async def screenshot(self, path: str) -> Dict[str, Any]:
        await self._ensure_browser()
        target, guard_err = _guard_screenshot_path(path)
        if guard_err is not None:
            return guard_err
        assert target is not None
        try:
            screenshot_bytes = await self._page.screenshot(full_page=False)
            if symlink_err := _reject_symlink_leaf(target, "save screenshot to"):
                return symlink_err
            atomic_write_bytes(target, bytes(screenshot_bytes), mode=None)
            return {
                "success": True,
                "path": str(target),
                "message": f"Screenshot saved to {target}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Screenshot failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def evaluate(self, js: str) -> Dict[str, Any]:
        await self._ensure_browser()
        self._begin_network_action()
        try:
            result = await self._page.evaluate(js)
            if blocked := self._blocked_request_result():
                return blocked
            return {"success": True, "result": result}
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"JavaScript evaluation failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def wait_for(
        self,
        text: Optional[str] = None,
        timeout_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        await self._ensure_browser()
        timeout = (timeout_ms or self._get_timeout() * 1000) / 1000.0
        self._begin_network_action()
        try:
            if text:
                await self._page.wait_for_selector(f"text={text}", timeout=timeout * 1000)
                if blocked := self._blocked_request_result():
                    return blocked
                return {"success": True, "message": f"Text '{text}' appeared on page."}
            await asyncio.sleep(timeout)
            if blocked := self._blocked_request_result():
                return blocked
            return {"success": True, "message": f"Waited {timeout:.1f}s."}
        except Exception as e:
            if blocked := self._blocked_request_result():
                return blocked
            return {
                "success": False,
                "error": f"Wait failed: {e}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }

    async def close(self) -> Dict[str, Any]:
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        self._ref_map.clear()
        self._blocked_request_reason = None
        self._initialized = False
        return {"success": True, "message": "Browser closed."}


class BrowserRegistry:
    """Registry of per-agent browser sessions for parallel sub-agent isolation."""

    _instance: Optional["BrowserRegistry"] = None
    _registry_lock = asyncio.Lock()

    def __init__(self) -> None:
        self._sessions: Dict[str, BrowserSession] = {}

    @classmethod
    def get(cls) -> "BrowserRegistry":
        if cls._instance is None:
            cls._instance = BrowserRegistry()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._instance = None

    async def for_agent(self, agent_id: Optional[str] = None) -> BrowserSession:
        from coderAI.core.execution_context import get_execution_context

        aid = agent_id or get_execution_context().agent_id or "main"
        async with self._registry_lock:
            session = self._sessions.get(aid)
            if session is None:
                session = BrowserSession(aid)
                self._sessions[aid] = session
            return session

    async def close_agent(self, agent_id: str) -> None:
        async with self._registry_lock:
            session = self._sessions.pop(agent_id, None)
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                logger.debug("BrowserSession[%s] close failed: %s", agent_id, exc)


class BrowserManager:
    """Backward-compatible facade delegating to the per-agent ``BrowserRegistry``."""

    async def _session(self) -> BrowserSession:
        return await BrowserRegistry.get().for_agent()

    async def navigate(self, url: str) -> Dict[str, Any]:
        return await (await self._session()).navigate(url)

    async def snapshot(self) -> str:
        return await (await self._session()).snapshot()

    async def click_by_ref(self, ref: str) -> Dict[str, Any]:
        return await (await self._session()).click_by_ref(ref)

    async def type_by_ref(self, ref: str, text: str, clear: bool = False) -> Dict[str, Any]:
        return await (await self._session()).type_by_ref(ref, text, clear=clear)

    async def select_option_by_ref(self, ref: str, value: str) -> Dict[str, Any]:
        return await (await self._session()).select_option_by_ref(ref, value)

    async def get_content(self, fmt: str = "markdown") -> str:
        return await (await self._session()).get_content(fmt=fmt)

    async def screenshot(self, path: str) -> Dict[str, Any]:
        return await (await self._session()).screenshot(path)

    async def evaluate(self, js: str) -> Dict[str, Any]:
        return await (await self._session()).evaluate(js)

    async def wait_for(
        self,
        text: Optional[str] = None,
        timeout_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        return await (await self._session()).wait_for(text=text, timeout_ms=timeout_ms)

    async def close(self) -> Dict[str, Any]:
        return await (await self._session()).close()


# ---------------------------------------------------------------------------
# Accessibility snapshot builder
# ---------------------------------------------------------------------------


async def _build_accessibility_snapshot(page: Any) -> Tuple[str, Dict[str, Dict[str, str]]]:
    """Walk Playwright's accessibility tree and produce a compact text
    representation with element references (``[e0]``, ``[e1]``, ...).

    Returns ``(snapshot_text, ref_map)`` where *ref_map* maps each ref to
    ``{"role": ..., "name": ...}`` for later lookup.
    """
    tree = await page.accessibility.snapshot()
    if tree is None:
        return "No accessibility tree available.", {}

    ref_counter = [0]
    ref_map: Dict[str, Dict[str, str]] = {}
    lines: List[str] = []

    def _walk(node: Dict[str, Any], depth: int) -> None:
        role = node.get("role", "unknown")
        name = (node.get("name") or "").strip()
        value = node.get("value")
        disabled = node.get("disabled", False)
        checked = node.get("checked")
        level = node.get("level")
        expanded = node.get("expanded")

        ref = f"e{ref_counter[0]}"
        ref_counter[0] += 1
        ref_map[ref] = {"role": role, "name": name}

        indent = "  " * depth
        tag_parts: List[str] = []

        tag_parts.append(f"[{ref}]")
        if disabled:
            tag_parts.append("[disabled]")
        if checked is not None:
            tag_parts.append("[checked]" if checked else "[unchecked]")
        if expanded is not None and role in ("combobox", "listbox", "menu", "tree", "treegrid"):
            tag_parts.append("[expanded]" if expanded else "[collapsed]")

        tag_parts.append(role)

        label = name
        if name:
            label += f' "{name}"'
        if value is not None and str(value).strip():
            label += f" = {value}"
        if level is not None:
            label += f" (level {level})"

        tag_parts.append(label)
        lines.append(indent + " ".join(tag_parts))

        for child in node.get("children") or []:
            _walk(child, depth + 1)

    _walk(tree, 0)
    return "\n".join(lines), ref_map


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_navigate
# ═══════════════════════════════════════════════════════════════════════════


class BrowserNavigateParams(BaseModel):
    url: str = Field(..., description="Fully-qualified URL to navigate to (https://...).")


class BrowserNavigateTool(Tool):
    """Navigate the browser to a URL."""

    name = "browser_navigate"
    description = (
        "Navigate the browser to a URL. Returns the page title and final URL "
        "after any redirects. Use this as the first step in any browser workflow. "
        "The browser session persists across calls — navigate, snapshot, click, "
        "and type all share the same page."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserNavigateParams
    timeout = 45.0
    safe = True
    is_egress = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL

    async def execute(self, url: str) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        if err := await _validate_navigation_url(url):
            return err
        if err := _check_domain_allowlist(url):
            return err
        return await BrowserManager().navigate(url)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_snapshot
# ═══════════════════════════════════════════════════════════════════════════


class BrowserSnapshotParams(BaseModel):
    pass  # no parameters


class BrowserSnapshotTool(Tool):
    """Get an accessibility snapshot of the current browser page."""

    name = "browser_snapshot"
    description = (
        "Capture the accessibility tree of the current browser page as a "
        "compact text representation. Each interactive element gets a unique "
        "ref like [e12] that you can use with browser_click / browser_type. "
        "Elements show their role ('button', 'textbox', 'link', 'combobox', "
        "'checkbox', etc.), accessible name, and current value/state. "
        "Call this after navigating or after performing actions to see "
        "the updated page state. Use this to identify form fields, buttons, "
        "links, and dropdowns before interacting with them."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserSnapshotParams
    is_read_only = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 15.0

    async def execute(self) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        try:
            text = await BrowserManager().snapshot()
            return {"success": True, "snapshot": text}
        except RuntimeError as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_click
# ═══════════════════════════════════════════════════════════════════════════


class BrowserClickParams(BaseModel):
    ref: str = Field(
        ...,
        description="Element reference from browser_snapshot (e.g. 'e12').",
    )


class BrowserClickTool(Tool):
    """Click an element on the page using its snapshot reference."""

    name = "browser_click"
    description = (
        "Click an interactive element identified by its ref from the most "
        "recent browser_snapshot. Works for buttons, links, checkboxes, "
        "radio buttons, tabs, and any clickable element. After clicking, "
        "call browser_snapshot to see the updated page state."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserClickParams
    requires_confirmation = True
    is_egress = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 20.0

    async def execute(self, ref: str) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        return await BrowserManager().click_by_ref(ref)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_type
# ═══════════════════════════════════════════════════════════════════════════


class BrowserTypeParams(BaseModel):
    ref: str = Field(
        ...,
        description="Element reference from browser_snapshot (e.g. 'e5'). Must be a textbox, searchbox, or combobox.",
    )
    text: str = Field(..., description="Text to type into the element.")
    clear: bool = Field(
        False, description="Clear existing text before typing (default: false, appends)."
    )


class BrowserTypeTool(Tool):
    """Type text into an input element on the page."""

    name = "browser_type"
    description = (
        "Type text into an input element (textbox, searchbox, textarea, "
        "combobox) identified by its ref from the most recent browser_snapshot. "
        "Set clear=true to replace existing text instead of appending. "
        "After typing, call browser_snapshot to see the updated state."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserTypeParams
    requires_confirmation = True
    is_egress = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 20.0

    async def execute(self, ref: str, text: str, clear: bool = False) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        return await BrowserManager().type_by_ref(ref, text, clear=clear)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_select_option
# ═══════════════════════════════════════════════════════════════════════════


class BrowserSelectOptionParams(BaseModel):
    ref: str = Field(
        ...,
        description="Element reference from browser_snapshot for a combobox or listbox.",
    )
    value: str = Field(
        ...,
        description="Option value or visible label to select.",
    )


class BrowserSelectOptionTool(Tool):
    """Select an option from a dropdown/combobox/listbox."""

    name = "browser_select_option"
    description = (
        "Select an option by value or visible label in a combobox, listbox, "
        "or dropdown identified by its ref from the most recent browser_snapshot. "
        "Use this for <select> elements, country pickers, size/color dropdowns, etc."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserSelectOptionParams
    requires_confirmation = True
    is_egress = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 20.0

    async def execute(self, ref: str, value: str) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        return await BrowserManager().select_option_by_ref(ref, value)


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_get_content
# ═══════════════════════════════════════════════════════════════════════════


class BrowserGetContentParams(BaseModel):
    fmt: str = Field(
        "markdown",
        description="Output format: 'markdown' (default), 'text', or 'html'.",
    )


class BrowserGetContentTool(Tool):
    """Extract the current page content as markdown, plain text, or HTML."""

    name = "browser_get_content"
    description = (
        "Extract the full text content of the current browser page. "
        "Use 'markdown' (default) for readable formatted text, 'text' for "
        "plain text, or 'html' for the raw HTML source. Useful for reading "
        "page content after navigating (e.g. product details, form summaries, "
        "confirmation pages)."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserGetContentParams
    is_read_only = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 15.0

    async def execute(self, fmt: str = "markdown") -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        if fmt not in ("markdown", "text", "html"):
            return {
                "success": False,
                "error": f"Invalid format: {fmt}. Use 'markdown', 'text', or 'html'.",
            }
        try:
            content = await BrowserManager().get_content(fmt=fmt)
            return {"success": True, "content": content}
        except RuntimeError as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_screenshot
# ═══════════════════════════════════════════════════════════════════════════


class BrowserScreenshotParams(BaseModel):
    path: str = Field(
        ...,
        description="Project-scoped file path to save the screenshot (e.g. 'artifacts/page.png').",
    )


class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current browser page."""

    name = "browser_screenshot"
    description = (
        "Take a screenshot of the current browser page and save it to the "
        "specified path (PNG format). Useful for capturing visual state, "
        "confirmation pages, error states, or when the accessibility tree "
        "doesn't capture the full picture."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserScreenshotParams
    requires_confirmation = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 20.0

    async def execute(self, path: str) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        try:
            return await BrowserManager().screenshot(path)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_evaluate
# ═══════════════════════════════════════════════════════════════════════════


class BrowserEvaluateParams(BaseModel):
    js: str = Field(
        ...,
        description="JavaScript code to execute in the page context. "
        "The return value is serialized and returned.",
    )


class BrowserEvaluateTool(Tool):
    """Execute arbitrary JavaScript in the browser page context."""

    name = "browser_evaluate"
    description = (
        "Execute JavaScript code in the current browser page and return the "
        "result. Useful for extracting data that isn't exposed in the "
        "accessibility tree, checking page state, or triggering custom "
        "behavior. The return value is JSON-serialized."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserEvaluateParams
    # Runs arbitrary JavaScript in the page: NOT read-only (it can mutate the DOM,
    # submit forms, or issue in-page fetches — a second SSRF/exfil path), so it
    # requires confirmation and is excluded from any read-only tool set (6.3).
    is_read_only = False
    requires_confirmation = True
    is_egress = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 15.0

    async def execute(self, js: str) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        try:
            return await BrowserManager().evaluate(js)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_wait
# ═══════════════════════════════════════════════════════════════════════════


class BrowserWaitParams(BaseModel):
    text: Optional[str] = Field(
        None,
        description="Wait until this text appears on the page.",
    )
    timeout_ms: Optional[float] = Field(
        None,
        description="Wait duration in milliseconds (default: browser timeout from config).",
    )


class BrowserWaitTool(Tool):
    """Wait for a condition on the page (text appearance or timeout)."""

    name = "browser_wait"
    description = (
        "Wait for a condition on the current page. If 'text' is provided, "
        "waits until that text appears in the DOM. If only 'timeout_ms' is "
        "provided, simply pauses for that duration. Useful after clicking "
        "navigation elements or submitting forms to let the page update."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserWaitParams
    is_read_only = True
    result_provenance = Provenance.UNTRUSTED_EXTERNAL
    timeout = 60.0

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        text = kwargs.get("text")
        timeout_ms = kwargs.get("timeout_ms")
        if err := _check_playwright():
            return err
        try:
            return await BrowserManager().wait_for(text=text, timeout_ms=timeout_ms)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Tool: browser_close
# ═══════════════════════════════════════════════════════════════════════════


class BrowserCloseParams(BaseModel):
    pass  # no parameters


class BrowserCloseTool(Tool):
    """Close the browser session and free resources."""

    name = "browser_close"
    description = (
        "Close the browser, freeing system resources. Call this when you're "
        "done with a browser workflow. The browser can be re-opened later with "
        "browser_navigate."
    )
    category = "browser"
    requires_package = "playwright"
    parameters_model = BrowserCloseParams
    timeout = 15.0
    # Only tears down the local browser session and frees resources — no
    # external effect — so it runs without per-call confirmation.
    safe = True

    async def execute(self) -> Dict[str, Any]:  # type: ignore[override]
        if err := _check_playwright():
            return err
        return await BrowserManager().close()
