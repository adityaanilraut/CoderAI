# mypy: disable-error-code="attr-defined, no-any-return"
"""Connection/session lifecycle and JSON-RPC dispatch for MCP."""

import asyncio
import logging
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.tools.mcp import MCPAuthRequiredError

logger = logging.getLogger("coderAI.tools.mcp")


class MCPSession:
    servers: dict[str, dict[str, Any]]
    discovered_tools: list[dict[str, Any]]
    discovered_resources: list[dict[str, Any]]
    discovered_prompts: list[dict[str, Any]]

    def __init__(self):
        """Initialize MCP client."""
        self.servers: dict[str, dict[str, Any]] = {}
        self.discovered_tools: list[dict[str, Any]] = []
        self.discovered_resources: list[dict[str, Any]] = []
        self.discovered_prompts: list[dict[str, Any]] = []
        self._next_id: int = 1
        self._reconnect_attempts: dict[str, int] = {}
        self._roots: list[dict[str, str]] = []
        self._schemas_dirty: bool = False
        # Optional async callable(server_name, params) -> elicitation result dict
        self.elicitation_handler: Optional[Any] = None
        # Optional async callable(server_name, params) -> sampling result dict
        self.sampling_handler: Optional[Any] = None
        self._set_default_roots()

    def _set_default_roots(self, project_root: Optional[str | Path] = None) -> None:
        root = Path(project_root or ".").resolve()
        self._roots = [{"uri": root.as_uri(), "name": root.name or "project"}]

    def set_project_root(self, project_root: str | Path) -> None:
        """Update advertised MCP roots and notify connected servers."""
        old = list(self._roots)
        self._set_default_roots(project_root)
        if old != self._roots:
            for name, entry in list(self.servers.items()):
                try:
                    asyncio.get_running_loop().create_task(self._notify_roots_changed(name, entry))
                except RuntimeError:
                    pass

    def _get_next_id(self) -> int:
        """Return a unique, incrementing JSON-RPC request ID."""
        current = self._next_id
        self._next_id += 1
        return current

    @staticmethod
    def _fail_pending(entry: dict[str, Any], error: BaseException) -> None:
        """Fail all requests waiting on a transport that reached EOF or closed."""
        pending = entry.get("pending", {})
        for future in list(pending.values()):
            if not future.done():
                future.set_exception(error)
        pending.clear()

    def _dispatch_response(
        self, server_name: str, entry: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """Route JSON-RPC responses, notifications, and server→client requests."""
        response_id = response.get("id")
        method = response.get("method")

        # Server → client request (has method + id, no result/error yet)
        if (
            method
            and response_id is not None
            and "result" not in response
            and "error" not in response
        ):
            try:
                asyncio.get_running_loop().create_task(
                    self._handle_server_request(server_name, entry, response)
                )
            except RuntimeError:
                logger.debug("No event loop to handle MCP server request %s", method)
            return

        # Notification (method, no id)
        if method and response_id is None:
            try:
                asyncio.get_running_loop().create_task(
                    self._handle_notification(
                        server_name, entry, method, response.get("params") or {}
                    )
                )
            except RuntimeError:
                logger.debug("No event loop to handle MCP notification %s", method)
            return

        if response_id is None:
            return
        future = entry.get("pending", {}).get(response_id)
        if future is None:
            logger.debug("Ignoring late or unknown MCP response id %r", response_id)
            return
        if not future.done():
            future.set_result(response)

    async def _handle_notification(
        self,
        server_name: str,
        entry: dict[str, Any],
        method: str,
        params: Any,
    ) -> None:
        if method == "notifications/progress":
            import time

            entry["_last_progress"] = time.monotonic()
            return
        if method in (
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        ):
            try:
                await self._rediscover_server(server_name, entry)
                self._schemas_dirty = True
            except Exception:
                logger.warning(
                    "Failed to refresh discovery after %s from '%s'",
                    method,
                    server_name,
                    exc_info=True,
                )
            return
        logger.debug("Ignoring MCP notification %s from '%s'", method, server_name)

    async def _handle_server_request(
        self,
        server_name: str,
        entry: dict[str, Any],
        request: dict[str, Any],
    ) -> None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        try:
            if method == "roots/list":
                result: dict[str, Any] = {"roots": list(self._roots)}
            elif method == "ping":
                result = {}
            elif method == "elicitation/create":
                result = await self._handle_elicitation(server_name, params)
            elif method == "sampling/createMessage":
                result = await self._handle_sampling(server_name, params)
            else:
                await self._send_raw(
                    entry,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    },
                )
                return
            await self._send_raw(entry, {"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            logger.warning("MCP server request %s failed: %s", method, exc, exc_info=True)
            with suppress(Exception):
                await self._send_raw(
                    entry,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(exc)[:500]},
                    },
                )

    async def _handle_elicitation(self, server_name: str, params: Any) -> dict[str, Any]:
        handler = self.elicitation_handler
        if handler is None:
            return {"action": "cancel", "content": {}}
        try:
            result = await handler(server_name, params if isinstance(params, dict) else {})
            if isinstance(result, dict) and result.get("action") in ("accept", "decline", "cancel"):
                return result
        except Exception:
            logger.warning("MCP elicitation handler failed for '%s'", server_name, exc_info=True)
        return {"action": "cancel", "content": {}}

    async def _handle_sampling(self, server_name: str, params: Any) -> dict[str, Any]:
        """Handle MCP sampling/createMessage request from a connected server."""
        handler = self.sampling_handler
        if handler is not None:
            try:
                result = await handler(server_name, params if isinstance(params, dict) else {})
                if isinstance(result, dict) and "content" in result:
                    return result
            except Exception:
                logger.warning(
                    "MCP custom sampling handler failed for '%s'", server_name, exc_info=True
                )

        if not isinstance(params, dict):
            raise ValueError("sampling/createMessage params must be an object")

        raw_messages = params.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("sampling/createMessage requires non-empty messages array")

        system_prompt = params.get("systemPrompt")
        max_tokens = int(params.get("maxTokens") or 1024)
        temperature = float(params.get("temperature", 0.7))

        llm_messages: list[dict[str, Any]] = []
        if system_prompt:
            llm_messages.append({"role": "system", "content": str(system_prompt)})

        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content")
            if isinstance(content, dict):
                text_val = content.get("text", "")
            elif isinstance(content, str):
                text_val = content
            else:
                text_val = str(content)
            llm_messages.append({"role": role, "content": text_val})

        try:
            from coderAI.core.services import get_services
            from coderAI.llm.factory import create_provider

            cfg = get_services().config
            model_name = getattr(cfg, "default_model", "claude-sonnet-5") or "claude-sonnet-5"
            model_prefs = params.get("modelPreferences")
            if isinstance(model_prefs, dict):
                hints = model_prefs.get("hints")
                if isinstance(hints, list) and hints:
                    first_hint = hints[0]
                    if isinstance(first_hint, dict) and first_hint.get("name"):
                        model_name = str(first_hint["name"])

            provider = create_provider(model_name, cfg)
            response = await provider.complete(
                messages=llm_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            reply_text = (
                response
                if isinstance(response, str)
                else getattr(response, "content", str(response))
            )

            return {
                "role": "assistant",
                "content": {
                    "type": "text",
                    "text": reply_text,
                },
                "model": model_name,
                "stopReason": "endTurn",
            }
        except Exception as exc:
            logger.warning("MCP default sampling completion failed for '%s': %s", server_name, exc)
            return {
                "role": "assistant",
                "content": {
                    "type": "text",
                    "text": f"[Sampling completion unavailable: {exc}]",
                },
                "model": "fallback",
                "stopReason": "other",
            }

    async def _notify_roots_changed(self, server_name: str, entry: dict[str, Any]) -> None:
        with suppress(Exception):
            await self._send_raw(
                entry,
                {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
            )

    async def _request(
        self,
        server_name: str,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request to a connected server and return its result.

        Dispatches across the stdio / SSE / HTTP transports using the same
        primitives as :meth:`call_tool`. Returns
        ``{"success": True, "result": <result dict>}`` on success, or
        ``{"success": False, "error": <message>}`` otherwise.
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}
        return await self._request_entry(
            server_name, self.servers[server_name], method, params, timeout
        )

    async def _request_entry(
        self,
        server_name: str,
        server: dict[str, Any],
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Send one request against an active or not-yet-committed connection entry."""

        transport = server.get("transport", "stdio")
        req_id = self._get_next_id()
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params

        try:
            if transport == "sse":
                response = await self._sse_exchange(server, request, timeout)
            elif transport == "http":
                session = server.get("session")
                url = server.get("url")
                if not session or not url:
                    return {
                        "success": False,
                        "error": f"HTTP connection state invalid for '{server_name}'",
                    }
                response = await self._http_send_with_reauth(
                    server_name, server, session, url, request, req_id, timeout
                )
            else:
                response = await self._stdio_exchange(server, request, timeout=timeout)
        except MCPAuthRequiredError:
            return {
                "success": False,
                "needs_auth": True,
                "error": (
                    f"Authorization expired for '{server_name}'. "
                    f"Run: coderAI mcp login {server_name}"
                ),
            }
        except asyncio.TimeoutError:
            if transport == "http":
                self._schedule_request_cancellation(server, req_id)
            return {
                "success": False,
                "error": f"Request '{method}' to '{server_name}' timed out after {timeout}s",
            }
        except asyncio.CancelledError:
            if transport == "http":
                self._schedule_request_cancellation(server, req_id)
            raise
        except Exception as e:
            return {"success": False, "error": str(e), "error_code": ToolErrorCode.TOOL_ERROR}

        if not isinstance(response, dict):
            return {"success": False, "error": f"MCP {method} returned a non-object response"}
        error = response.get("error")
        if error:
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            return {"success": False, "error": message}
        result = response.get("result", {})
        if not isinstance(result, dict):
            return {"success": False, "error": f"MCP {method} returned a non-object result"}
        return {"success": True, "result": result}

    async def _close_server_entry(self, server: dict[str, Any], *, force: bool = False) -> None:
        """Close one transport entry and surface cleanup failures to the caller."""
        from coderAI.tools import mcp as _mcp

        errors: list[str] = []
        self._fail_pending(server, RuntimeError("MCP connection closed"))
        tasks = [server.get("reader_task"), server.get("stderr_task")]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()

        transport = server.get("transport", "stdio")
        if transport in ("sse", "http"):
            response = server.get("sse_response")
            if response is not None:
                with suppress(Exception):
                    response.close()
            session = server.get("session")
            if session:
                try:
                    await session.close()
                except Exception as exc:
                    errors.append(f"failed to close {transport} session: {exc}")
        else:
            process = server.get("process")
            if process is not None and process.returncode is None:
                try:
                    if force:
                        _mcp.kill_process_group(process)
                    else:
                        _mcp.kill_process_group(process, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        _mcp.kill_process_group(process)
                        await process.wait()
                    except Exception as exc:
                        errors.append(f"failed to kill stdio process: {exc}")
                except Exception as exc:
                    errors.append(f"failed to stop stdio process: {exc}")

        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not None), return_exceptions=True
            )
        if errors:
            raise RuntimeError("; ".join(errors))

    async def disconnect(self, server_name: str) -> dict[str, Any]:
        """Disconnect from an MCP server.

        Args:
            server_name: Name of the server to disconnect from

        Returns:
            Result dictionary
        """
        if server_name not in self.servers:
            return {"success": False, "error": f"Server not connected: {server_name}"}

        server = self.servers.pop(server_name)
        self.discovered_tools = [t for t in self.discovered_tools if t.get("server") != server_name]
        self.discovered_resources = [
            r for r in self.discovered_resources if r.get("server") != server_name
        ]
        self.discovered_prompts = [
            p for p in self.discovered_prompts if p.get("server") != server_name
        ]

        try:
            await self._close_server_entry(server)
        except Exception as exc:
            return {
                "success": False,
                "error": f"Disconnected '{server_name}', but cleanup failed: {exc}",
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        return {"success": True, "message": f"Disconnected from {server_name}"}

    async def check_server_health(self):
        """Check if each connected MCP server is still alive.

        For stdio servers: checks if the subprocess has exited (returncode is not None).
        For SSE servers: sends an in-band MCP ``ping`` over the live session.
        Dead servers are marked with a ``degraded`` flag and a warning is logged.
        """

        async def _probe_sse(name: str, info: dict[str, Any]) -> None:
            """Liveness-probe one SSE server with an in-band MCP ``ping``.

            The probe reuses the connection's own session so its auth headers
            and cookies apply, and sends a real JSON-RPC request rather than an
            ``OPTIONS`` preflight — most MCP message endpoints accept POST only,
            so preflighting a perfectly healthy server returned 405 and got it
            marked degraded (dropping all of its tools and kicking off reconnect
            back-offs). A JSON-RPC error reply such as ``Method not found`` still
            proves the transport is alive, so only a raised exception (timeout,
            connection failure, non-2xx POST, dead event stream) degrades.
            """
            message_url = info.get("message_url")
            if not message_url:
                return
            session = info.get("session")
            if session is None or session.closed:
                if not info.get("degraded"):
                    logger.warning("MCP server '%s' (SSE) session is closed", name)
                    info["degraded"] = True
                return
            reader_task = info.get("reader_task")
            if reader_task is not None and reader_task.done():
                if not info.get("degraded"):
                    logger.warning("MCP server '%s' (SSE) event stream has ended", name)
                    info["degraded"] = True
                return
            try:
                await self._sse_exchange(
                    info,
                    {"jsonrpc": "2.0", "id": self._get_next_id(), "method": "ping"},
                    timeout=5,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not info.get("degraded"):
                    logger.warning(
                        "MCP server '%s' (SSE) health check failed: %s",
                        name,
                        e,
                    )
                    info["degraded"] = True

        sse_probes = []
        for name, info in list(self.servers.items()):
            transport = info.get("transport", "stdio")

            if transport == "stdio":
                process = info.get("process")
                if process is not None and process.returncode is not None:
                    if not info.get("degraded"):
                        logger.warning(
                            "MCP server '%s' (stdio) appears dead (returncode=%s)",
                            name,
                            process.returncode,
                        )
                        info["degraded"] = True
            elif transport == "sse":
                # Probe SSE servers concurrently — a slow/hung server must not
                # delay the health check for the others (each waits up to 5s).
                sse_probes.append(_probe_sse(name, info))
            elif transport == "http":
                # Streamable HTTP keeps a single aiohttp session; a closed
                # session means the connection is gone.
                session = info.get("session")
                if session is None or session.closed:
                    if not info.get("degraded"):
                        logger.warning("MCP server '%s' (HTTP) session is closed", name)
                        info["degraded"] = True

        if sse_probes:
            await asyncio.gather(*sse_probes, return_exceptions=True)

    async def auto_reconnect_degraded(self):
        """Attempt to reconnect degraded MCP servers.

        Tracks reconnect attempts per server (max 3) and uses exponential
        backoff between attempts. Clears the degraded flag on success.
        """
        import asyncio as _asyncio

        for name, info in list(self.servers.items()):
            if not info.get("degraded"):
                continue

            attempts = self._reconnect_attempts.get(name, 0)
            if attempts >= 3:
                logger.warning(
                    "MCP server '%s' reached max reconnect attempts (3), giving up",
                    name,
                )
                continue

            self._reconnect_attempts[name] = attempts + 1
            backoff = 2 ** (attempts + 1)
            logger.info(
                "Reconnecting MCP server '%s' (attempt %d/3, backoff %ds)…",
                name,
                attempts + 1,
                backoff,
            )
            await _asyncio.sleep(backoff)

            transport = info.get("transport", "stdio")
            conn_params = info.get("_conn_params", {})

            result: dict[str, Any]
            if transport == "sse":
                result = await self.connect_sse(name, conn_params.get("url", ""))
            elif transport == "http":
                result = await self.connect_http(
                    name, conn_params.get("url", ""), conn_params.get("headers")
                )
            else:
                result = await self.connect_stdio(
                    name,
                    conn_params.get("command", ""),
                    conn_params.get("args"),
                    env=conn_params.get("env"),
                    cwd=conn_params.get("cwd"),
                    timeout=float(conn_params.get("timeout") or 10),
                )

            if result.get("success"):
                self._reconnect_attempts.pop(name, None)
                # Reconnect can restore tools that were filtered out while the
                # server was degraded — mark schemas dirty so the next LLM call
                # rebuilds them even if the owning ExecutionLoop already ended.
                self._schemas_dirty = True
                logger.info("Successfully reconnected to MCP server '%s'", name)
            else:
                logger.warning(
                    "Failed to reconnect MCP server '%s': %s",
                    name,
                    result.get("error"),
                )
