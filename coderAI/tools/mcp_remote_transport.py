# mypy: disable-error-code="attr-defined, no-any-return"
"""SSE and Streamable HTTP transports for MCP."""

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Optional

from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.tools.mcp import (
    MCPAuthRequiredError,
    _reject_reserved_server_name,
    _validated_same_origin_url,
    validate_remote_mcp_url,
)

logger = logging.getLogger("coderAI.tools.mcp")


class MCPRemoteTransport:
    async def _send_raw(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        transport = entry.get("transport", "stdio")
        if transport == "stdio":
            await self._stdio_send(entry, payload)
            return
        if transport == "sse":
            import aiohttp

            session = entry.get("session")
            message_url = entry.get("message_url")
            if session and message_url:
                async with session.post(
                    message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        logger.debug("MCP server reply POST returned HTTP %s", response.status)
            return
        session = entry.get("session")
        url = entry.get("url")
        if session and url:
            await self._http_send(
                session,
                url,
                payload,
                expected_id=None,
                session_id=entry.get("session_id"),
                timeout=5,
            )

    def _schedule_request_cancellation(self, entry: dict[str, Any], request_id: Any) -> None:
        """Best-effort MCP cancellation without delaying local timeout/cancellation."""
        try:
            task = asyncio.create_task(self._send_request_cancellation(entry, request_id))
        except RuntimeError:
            return

        def _consume_result(done: "asyncio.Task[None]") -> None:
            with suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_consume_result)

    async def _send_request_cancellation(self, entry: dict[str, Any], request_id: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": "client request cancelled"},
        }
        transport = entry.get("transport", "stdio")
        if transport == "stdio":
            await self._stdio_send(entry, payload)
            return
        if transport == "sse":
            import aiohttp

            session = entry.get("session")
            message_url = entry.get("message_url")
            if session and message_url:
                async with session.post(
                    message_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=False,
                ) as response:
                    if response.status >= 400:
                        logger.debug("MCP cancellation returned HTTP %s", response.status)
            return
        session = entry.get("session")
        url = entry.get("url")
        if session and url:
            await self._http_send(
                session,
                url,
                payload,
                expected_id=None,
                session_id=entry.get("session_id"),
                timeout=5,
            )

    async def _sse_exchange(
        self, entry: dict[str, Any], request: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """POST to a legacy SSE endpoint and await the long-lived stream dispatcher."""
        import aiohttp

        session = entry.get("session")
        message_url = entry.get("message_url")
        if not session or not message_url:
            raise RuntimeError("SSE connection state is invalid")
        request_id = request.get("id")
        future = None
        if request_id is not None:
            future = asyncio.get_running_loop().create_future()
            if request_id in entry["pending"]:
                raise RuntimeError(f"Duplicate in-flight MCP request id {request_id!r}")
            entry["pending"][request_id] = future
        try:
            async with session.post(
                message_url,
                json=request,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as response:
                if response.status >= 300:
                    body = await response.text()
                    raise RuntimeError(
                        f"MCP SSE message endpoint returned HTTP {response.status}: {body[:300]}"
                    )
                if request_id is None:
                    return {}
                if "application/json" in response.headers.get("Content-Type", ""):
                    parsed = await response.json()
                    if isinstance(parsed, dict):
                        self._dispatch_response(entry.get("_server_name", ""), entry, parsed)
            assert future is not None
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if future is not None:
                future.cancel()
            if request_id is not None:
                self._schedule_request_cancellation(entry, request_id)
            raise
        finally:
            if request_id is not None:
                entry["pending"].pop(request_id, None)

    async def _refresh_session_token(self, server_name: str, server: dict[str, Any]) -> bool:
        """Re-stamp a Streamable HTTP session's bearer from the credential store.

        Returns ``True`` only when a *different* token was installed, so callers
        know a replay is worth attempting. A config-supplied ``Authorization``
        header is left alone: that is a static credential the user set explicitly,
        not something the OAuth store owns.
        """
        configured = server.get("headers") or {}
        if any(str(key).lower() == "authorization" for key in configured):
            return False
        session = server.get("session")
        headers = getattr(session, "headers", None)
        if headers is None:
            return False

        from coderAI.tools.mcp_oauth import get_valid_token_sync

        current = headers.get("Authorization")
        try:
            token = await asyncio.to_thread(get_valid_token_sync, server_name, force_refresh=True)
        except Exception:
            logger.debug("MCP token refresh failed for '%s'", server_name, exc_info=True)
            return False
        if not token:
            return False
        header = f"Bearer {token}"
        if header == current:
            return False
        try:
            headers["Authorization"] = header
        except Exception:
            logger.debug(
                "Could not update session headers for MCP server '%s'",
                server_name,
                exc_info=True,
            )
            return False
        logger.info("Refreshed OAuth token for MCP server '%s' after HTTP 401", server_name)
        return True

    async def _http_send_with_reauth(
        self,
        server_name: str,
        server: dict[str, Any],
        session: Any,
        url: str,
        request: dict[str, Any],
        req_id: int,
        timeout: float,
    ) -> dict[str, Any]:
        """POST over Streamable HTTP, refreshing an expired bearer once on 401.

        ``connect_http`` resolves the OAuth access token a single time, at connect,
        and bakes it into the session's default headers. Any session outliving that
        token then saw every call fail with "run ``coderAI mcp login``" even though
        a usable refresh token was sitting on disk. Refresh silently and replay the
        request instead; a second 401 propagates as before.
        """
        try:
            response, new_session_id = await self._http_send(
                session,
                url,
                request,
                expected_id=req_id,
                session_id=server.get("session_id"),
                timeout=timeout,
            )
        except MCPAuthRequiredError:
            if not await self._refresh_session_token(server_name, server):
                raise
            response, new_session_id = await self._http_send(
                session,
                url,
                request,
                expected_id=req_id,
                session_id=server.get("session_id"),
                timeout=timeout,
            )
        server["session_id"] = new_session_id
        return response or {}

    async def _sse_reader(self, server_name: str, entry: dict[str, Any], response: Any) -> None:
        """Keep the legacy SSE response open and dispatch complete events."""
        event_name = "message"
        data_lines: list[str] = []
        error: BaseException = RuntimeError(f"MCP SSE server '{server_name}' closed the stream")
        try:
            while True:
                line = await response.content.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text == "":
                    if data_lines:
                        data = "\n".join(data_lines)
                        if event_name == "endpoint":
                            future = entry.get("endpoint_future")
                            if future is not None and not future.done():
                                future.set_result(data)
                        else:
                            try:
                                parsed = json.loads(data)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "Ignoring malformed SSE event from '%s'", server_name
                                )
                            else:
                                if isinstance(parsed, dict):
                                    self._dispatch_response(server_name, entry, parsed)
                    event_name = "message"
                    data_lines = []
                    continue
                if text.startswith(":"):
                    continue
                field, _, value = text.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
        except asyncio.CancelledError:
            error = RuntimeError(f"MCP SSE reader for '{server_name}' was cancelled")
            raise
        except Exception as exc:
            error = RuntimeError(f"MCP SSE reader for '{server_name}' failed: {exc}")
            logger.debug("MCP SSE reader failed", exc_info=True)
        finally:
            endpoint_future = entry.get("endpoint_future")
            if endpoint_future is not None and not endpoint_future.done():
                endpoint_future.set_exception(error)
            self._fail_pending(entry, error)
            if self.servers.get(server_name) is entry:
                entry["degraded"] = True

    async def connect_sse(self, server_name: str, url: str) -> dict[str, Any]:
        """Connect to an MCP server via SSE transport.

        Args:
            server_name: Friendly name for this server connection
            url: SSE endpoint URL (e.g., http://localhost:8080/sse)

        Returns:
            Connection result with discovered tools
        """
        import aiohttp

        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        scheme_err = validate_remote_mcp_url(url)
        if scheme_err:
            return {"success": False, "error": scheme_err}

        candidate_entry: Optional[dict[str, Any]] = None
        committed = False
        try:
            session = aiohttp.ClientSession()
            response = await session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                allow_redirects=False,
            )
            if response.status != 200:
                raise RuntimeError(f"SSE endpoint returned HTTP {response.status}")
            endpoint_future = asyncio.get_running_loop().create_future()
            candidate_entry = {
                "transport": "sse",
                "session": session,
                "sse_response": response,
                "sse_url": url,
                "endpoint_future": endpoint_future,
                "pending": {},
                "_server_name": server_name,
                "_conn_params": {"url": url},
            }
            candidate_entry["reader_task"] = asyncio.create_task(
                self._sse_reader(server_name, candidate_entry, response)
            )
            advertised_endpoint = await asyncio.wait_for(
                asyncio.shield(endpoint_future), timeout=10
            )
            message_url = _validated_same_origin_url(url, advertised_endpoint)
            candidate_entry["message_url"] = message_url

            init_id = self._get_next_id()
            init_response = await self._sse_exchange(
                candidate_entry, self._init_request(init_id), timeout=10
            )
            await self._sse_exchange(
                candidate_entry,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=10,
            )

            tools_id = self._get_next_id()
            tools_response = await self._sse_exchange(
                candidate_entry,
                {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                timeout=10,
            )
            result = await self._finish_connect(
                server_name,
                candidate_entry,
                init_response,
                tools_response,
            )
            committed = True
            return result

        except ImportError:
            return {"success": False, "error": "aiohttp is required for SSE transport"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            if candidate_entry is not None and not committed:
                with suppress(Exception):
                    await self._close_server_entry(candidate_entry, force=True)

    async def connect_http(
        self,
        server_name: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Connect to an MCP server via Streamable HTTP transport.

        This is the modern remote-server transport (MCP spec 2025-03-26) that
        superseded HTTP+SSE: every JSON-RPC message is an HTTP POST to a single
        endpoint, and the server may answer either with a plain JSON body or an
        ``text/event-stream`` body carrying the response. A ``Mcp-Session-Id``
        header returned on ``initialize`` is echoed back on every later request.

        Args:
            server_name: Friendly name for this server connection.
            url: The single MCP endpoint URL (e.g. https://host/mcp).
            headers: Optional extra headers (e.g. ``Authorization``) sent on
                every request — used for token-authenticated remote servers.

        Returns:
            Connection result with discovered tools.
        """
        import aiohttp

        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        scheme_err = validate_remote_mcp_url(url)
        if scheme_err:
            return {"success": False, "error": scheme_err}

        candidate_entry: Optional[dict[str, Any]] = None
        committed = False
        try:
            base_headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if headers:
                base_headers.update(headers)

            # Inject a stored OAuth bearer token (refreshing silently if needed)
            # unless the caller already supplied an explicit Authorization header.
            if not any(k.lower() == "authorization" for k in base_headers):
                from coderAI.tools.mcp_oauth import get_valid_token_sync

                token = await asyncio.to_thread(get_valid_token_sync, server_name)
                if token:
                    base_headers["Authorization"] = f"Bearer {token}"

            session = aiohttp.ClientSession(headers=base_headers)
            candidate_entry = {
                "transport": "http",
                "session": session,
                "url": url,
                "session_id": None,
                "headers": headers or {},
                "_server_name": server_name,
                "pending": {},
                "_conn_params": {"url": url, "headers": headers or {}},
            }

            init_id = self._get_next_id()
            init_response, session_id = await self._http_send(
                session, url, self._init_request(init_id), expected_id=init_id, session_id=None
            )
            if init_response is None:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' returned no response to initialize",
                }

            # The session id (if any) must accompany every subsequent request.
            candidate_entry["session_id"] = session_id
            await self._http_send(
                session,
                url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                expected_id=None,
                session_id=session_id,
            )

            tools_id = self._get_next_id()
            tools_response, _ = await self._http_send(
                session,
                url,
                {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                expected_id=tools_id,
                session_id=session_id,
            )
            tools_response = tools_response or {}

            result = await self._finish_connect(
                server_name,
                candidate_entry,
                init_response,
                tools_response,
            )
            committed = True
            return result

        except MCPAuthRequiredError as e:
            return {
                "success": False,
                "needs_auth": True,
                "www_authenticate": e.www_authenticate,
                "error": (
                    f"MCP server '{server_name}' requires authorization. "
                    f"Run: coderAI mcp login {server_name}"
                ),
            }
        except ImportError:
            return {"success": False, "error": "aiohttp is required for HTTP transport"}
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            if candidate_entry is not None and not committed:
                with suppress(Exception):
                    await self._close_server_entry(candidate_entry, force=True)

    async def _http_send(
        self,
        session: Any,
        url: str,
        payload: dict[str, Any],
        expected_id: Optional[int],
        session_id: Optional[str],
        timeout: float = 30,
    ) -> tuple:
        """POST one JSON-RPC message over Streamable HTTP and read the reply.

        Returns ``(response_dict_or_None, session_id)``. ``expected_id`` is
        ``None`` for notifications (the server replies 202/empty and we return
        ``None``). The server's ``Mcp-Session-Id`` header — present on the
        ``initialize`` reply — is threaded back out so callers can reuse it.
        """
        import aiohttp

        req_headers = {}
        if session_id:
            req_headers["Mcp-Session-Id"] = session_id
        async with session.post(
            url,
            json=payload,
            headers=req_headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as resp:
            new_session_id = resp.headers.get("Mcp-Session-Id") or session_id
            if resp.status == 401:
                raise MCPAuthRequiredError(resp.headers.get("WWW-Authenticate"))
            if resp.status >= 300:
                body = await resp.text()
                raise RuntimeError(f"MCP server returned HTTP {resp.status}: {body[:300]}")
            # Notifications expect no response body (202 Accepted is typical).
            if expected_id is None:
                return None, new_session_id
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                parsed = await self._read_http_sse(resp, expected_id, timeout=timeout)
            else:
                parsed = await resp.json()
            response_id = parsed.get("id") if isinstance(parsed, dict) else None
            if not isinstance(parsed, dict) or response_id != expected_id:
                raise RuntimeError(
                    f"MCP server returned response id {response_id!r}; expected {expected_id!r}"
                )
            return parsed, new_session_id

    async def _read_http_sse(
        self, resp: Any, expected_id: int, timeout: float = 30
    ) -> dict[str, Any]:
        """Read an SSE-framed HTTP body until the matching JSON-RPC reply lands.

        Streamable HTTP servers may answer a single request with an event
        stream that interleaves server notifications before the actual result;
        we accumulate ``data:`` lines per event and return the first event whose
        JSON-RPC ``id`` matches ``expected_id``.
        """
        import time

        deadline = time.monotonic() + timeout
        data_lines: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            line = await asyncio.wait_for(resp.content.readline(), timeout=remaining)
            if not line:
                raise RuntimeError("Server closed the event stream before responding")
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if text == "":
                # Blank line dispatches the buffered event.
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        parsed = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(parsed, dict) and parsed.get("id") == expected_id:
                        return parsed
                continue
            if text.startswith(":"):
                continue  # SSE comment / keep-alive
            if text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
