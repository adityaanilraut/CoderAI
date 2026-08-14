# mypy: disable-error-code="attr-defined, no-any-return"
"""Stdio process transport for MCP."""

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Optional, cast
from pathlib import Path

from coderAI.system.proc import new_session_kwargs
from coderAI.system.sandbox import prepare_sandbox_launch
from coderAI.types.tool_error_codes import ToolErrorCode
from coderAI.tools.mcp import _reject_reserved_server_name, validate_stdio_launch

logger = logging.getLogger("coderAI.tools.mcp")


class MCPStdioTransport:
    async def _stdio_reader(
        self, server_name: str, entry: dict[str, Any], stdout: asyncio.StreamReader
    ) -> None:
        """Sole stdout reader for a stdio connection; dispatch replies by JSON-RPC ID."""
        error: BaseException = RuntimeError(f"MCP server '{server_name}' closed stdout")
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    parsed = json.loads(line.decode("utf-8", errors="replace"))
                except (UnicodeError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed JSON from MCP server '%s'", server_name)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("Ignoring non-object JSON from MCP server '%s'", server_name)
                    continue
                self._dispatch_response(server_name, entry, parsed)
        except asyncio.CancelledError:
            error = RuntimeError(f"MCP server '{server_name}' reader was cancelled")
            raise
        except Exception as exc:
            error = RuntimeError(f"MCP server '{server_name}' stdout reader failed: {exc}")
            logger.debug("MCP stdio reader failed", exc_info=True)
        finally:
            self._fail_pending(entry, error)
            if self.servers.get(server_name) is entry:
                entry["degraded"] = True

    async def _stdio_send(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        process = entry.get("process")
        if process is None or process.returncode is not None or process.stdin is None:
            raise RuntimeError("MCP stdio process is not running")
        async with entry["write_lock"]:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()

    async def _stdio_exchange(
        self, entry: dict[str, Any], request: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        """Register a request future before writing, then await dispatcher delivery.

        Progress notifications reset the idle wait so long-running tools that
        emit ``notifications/progress`` are not aborted early.
        """
        import time

        request_id = request.get("id")
        if request_id is None:
            await self._stdio_send(entry, request)
            return {}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        pending = entry["pending"]
        if request_id in pending:
            raise RuntimeError(f"Duplicate in-flight MCP request id {request_id!r}")
        pending[request_id] = future
        entry["_last_progress"] = time.monotonic()
        try:
            await self._stdio_send(entry, request)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                # Poll in chunks so progress can extend the overall deadline.
                chunk = min(remaining, 1.0)
                try:
                    return cast(
                        dict[str, Any],
                        await asyncio.wait_for(asyncio.shield(future), timeout=chunk),
                    )
                except asyncio.TimeoutError:
                    if future.done():
                        return cast(dict[str, Any], future.result())
                    last = float(entry.get("_last_progress") or 0)
                    # Extend deadline when progress arrived since the request started.
                    if last > deadline - timeout:
                        deadline = max(deadline, last + timeout)
                        continue
                    if time.monotonic() >= deadline:
                        raise
        except (asyncio.TimeoutError, asyncio.CancelledError):
            future.cancel()
            self._schedule_request_cancellation(entry, request_id)
            raise
        finally:
            pending.pop(request_id, None)

    async def _drain_stderr(self, server_name: str, stream: asyncio.StreamReader) -> None:
        """Continuously read a stdio server's stderr so its pipe never fills.

        ``stderr`` is a PIPE but nothing else reads it. The OS pipe buffer is
        small (~64KB); once a chatty server fills it, its next write to stderr
        blocks, and because stdio MCP servers are typically single-threaded,
        that also stalls the stdout responses we read — deadlocking the
        connection. Draining to the debug log keeps the buffer clear while
        preserving server diagnostics.
        """
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[mcp:%s stderr] %s", server_name, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "stderr drain for MCP server '%s' ended unexpectedly", server_name, exc_info=True
            )

    async def connect_stdio(
        self,
        server_name: str,
        command: str,
        args: Optional[list[str]] = None,
        *,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str | Path] = None,
        timeout: float = 10,
    ) -> dict[str, Any]:
        """Connect to an MCP server via stdio transport.

        Args:
            server_name: Friendly name for this server connection
            command: Server command to run (e.g., 'npx', 'python3')
            args: Command line arguments for the server
            env: Optional env overlays applied after :func:`scrub_env` (explicit keys only)
            cwd: Working directory for the server process (default: project cwd)
            timeout: Seconds to wait for initialize / tools/list

        Returns:
            Connection result with discovered tools
        """
        from coderAI.tools.mcp_config import build_stdio_env, resolve_mcp_cwd

        reject = _reject_reserved_server_name(server_name)
        if reject:
            return reject

        # Single launcher-validation choke point: applies to both LLM-driven
        # ``mcp_connect`` and config-driven autoconnect (which calls us directly).
        launch_err = validate_stdio_launch(command, args)
        if launch_err:
            return {"success": False, "error": launch_err}

        process = None
        candidate_entry: Optional[dict[str, Any]] = None
        connection_failed = True
        workdir = resolve_mcp_cwd(str(cwd) if cwd is not None else None)
        child_env = build_stdio_env(env)
        # Advertise project dir for presets that reference ${CODERAI_PROJECT_DIR}.
        child_env.setdefault("CODERAI_PROJECT_DIR", str(Path(".").resolve()))
        try:
            # On Windows ``create_subprocess_exec`` does not consult PATHEXT, so
            # a bare ``npx``/``npm``/``node`` won't resolve to its ``.cmd``/``.exe``
            # launcher the way it does on POSIX. Resolve via ``shutil.which``
            # (which honours PATHEXT) so npx-based MCP servers can start.
            launch_command = command
            if os.name == "nt":
                resolved = shutil.which(command)
                if resolved:
                    launch_command = resolved
            if not shutil.which(launch_command) and not os.path.exists(launch_command):
                raise FileNotFoundError(f"Command not found: {command}")
            full_args = [launch_command] + (args or [])
            launch = prepare_sandbox_launch(full_args, cwd=workdir)
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=child_env,
                **new_session_kwargs(),
            )
            assert process.stdin is not None
            assert process.stdout is not None

            stderr_task: Optional["asyncio.Task[None]"] = None
            if process.stderr is not None:
                stderr_task = asyncio.create_task(self._drain_stderr(server_name, process.stderr))
            candidate_entry = {
                "transport": "stdio",
                "process": process,
                "stderr_task": stderr_task,
                "pending": {},
                "write_lock": asyncio.Lock(),
                "_server_name": server_name,
                "_conn_params": {
                    "command": command,
                    "args": args,
                    "env": dict(env) if env else None,
                    "cwd": str(workdir),
                    "timeout": timeout,
                },
            }
            candidate_entry["reader_task"] = asyncio.create_task(
                self._stdio_reader(server_name, candidate_entry, process.stdout)
            )

            init_id = self._get_next_id()
            try:
                init_response = await self._stdio_exchange(
                    candidate_entry, self._init_request(init_id), timeout=timeout
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": (
                        f"Server '{server_name}' did not respond to initialize within {timeout:g}s"
                    ),
                }

            # If preferred protocol is rejected, retry once with the fallback version.
            if isinstance(init_response, dict) and init_response.get("error"):
                err_msg = str(init_response.get("error"))
                logger.info(
                    "MCP initialize preferred version failed for '%s': %s; trying fallback",
                    server_name,
                    err_msg,
                )
                init_id = self._get_next_id()
                try:
                    init_response = await self._stdio_exchange(
                        candidate_entry,
                        self._init_request(init_id, self.FALLBACK_PROTOCOL_VERSION),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return {
                        "success": False,
                        "error": (
                            f"Server '{server_name}' did not respond to initialize "
                            f"within {timeout:g}s"
                        ),
                    }

            await self._stdio_send(
                candidate_entry,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            )

            tools_id = self._get_next_id()
            try:
                tools_response = await self._stdio_exchange(
                    candidate_entry,
                    {"jsonrpc": "2.0", "id": tools_id, "method": "tools/list"},
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"Server '{server_name}' did not respond to tools/list",
                }

            result = await self._finish_connect(
                server_name,
                candidate_entry,
                init_response,
                tools_response,
            )
            connection_failed = False
            return result

        except FileNotFoundError:
            return {
                "success": False,
                "error": f"Command not found: {command}. Is the MCP server installed?",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": ToolErrorCode.TOOL_ERROR,
            }
        finally:
            if candidate_entry is not None and connection_failed:
                try:
                    await self._close_server_entry(candidate_entry, force=True)
                except Exception:
                    logger.debug(
                        "Failed to close MCP candidate in connect_stdio finally", exc_info=True
                    )
