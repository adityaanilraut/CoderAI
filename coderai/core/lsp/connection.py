"""LSP Connection Layer — JSON-RPC 2.0 transport over child process stdio."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from typing import Any
from collections.abc import Callable

from coderai.core.lsp.protocol import LspFrameParser, encode_lsp_message

logger = logging.getLogger(__name__)


class LspConnection:
    """Manages full-duplex JSON-RPC 2.0 communication with an LSP server subprocess over stdio."""

    def __init__(self, command: list[str], cwd: str, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen[bytes] | None = None
        self._next_request_id = 1
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notification_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._reader_thread: threading.Thread | None = None
        self._parser = LspFrameParser()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._lock = threading.Lock()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None and not self._closed

    def start(self) -> None:
        """Spawn the language server process and start reader thread."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        run_env = os.environ.copy()
        if self.env:
            run_env.update(self.env)

        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=run_env,
        )

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name=f"lsp-reader-{self.command[0]}",
            daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._read_stderr_loop,
            name=f"lsp-stderr-{self.command[0]}",
            daemon=True,
        )
        self._stderr_thread.start()

    def _read_stderr_loop(self) -> None:
        """Background thread draining stderr to prevent pipe buffer deadlocks."""
        stderr = self.process.stderr if self.process else None
        if not stderr:
            return
        while not self._closed and self.process and self.process.poll() is None:
            try:
                line = stderr.readline()
                if not line:
                    break
                logger.debug(f"[LSP stderr] {line.decode('utf-8', errors='replace').rstrip()}")
            except Exception:
                break

    def _read_loop(self) -> None:
        """Background thread reading framed bytes from server stdout."""
        stdout = self.process.stdout if self.process else None
        if not stdout:
            return

        while not self._closed and self.process and self.process.poll() is None:
            try:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                messages = self._parser.feed(chunk)
                for msg in messages:
                    self._dispatch_message(msg)
            except Exception as exc:
                logger.debug(f"LSP reader error: {exc}")
                break

        self._cleanup()

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Dispatch an incoming JSON-RPC message to its matching request future, notification handler, or reply to server requests."""
        req_id = message.get("id")
        method = message.get("method")

        if req_id is not None and method:
            # Server-to-client request: notify handlers, and reply if unhandled
            handled = False
            for handler in self._notification_handlers:
                try:
                    handler(message)
                    handled = True
                except Exception:
                    pass
            if not handled:
                # Respond with null result to satisfy the server request
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": None,
                }
                try:
                    if self.process and self.process.stdin:
                        self.process.stdin.write(encode_lsp_message(reply))
                        self.process.stdin.flush()
                except Exception:
                    pass
            return

        if req_id is not None:
            # Response to a client request
            with self._lock:
                fut = self._pending_requests.pop(req_id, None)
                if fut is None:
                    try:
                        fut = self._pending_requests.pop(int(req_id), None)
                    except (ValueError, TypeError):
                        pass
            if fut and not fut.done() and self._loop:
                self._loop.call_soon_threadsafe(fut.set_result, message)
        else:
            # Notification
            for handler in self._notification_handlers:
                try:
                    handler(message)
                except Exception:
                    pass

    async def send_request(
        self, method: str, params: Any = None, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the corresponding response."""
        if not self.is_alive():
            raise RuntimeError(f"LSP server '{self.command[0]}' is not running")

        with self._lock:
            req_id = self._next_request_id
            self._next_request_id += 1
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending_requests[req_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params if params is not None else {},
        }

        wire_bytes = encode_lsp_message(payload)
        try:
            if self.process and self.process.stdin:
                self.process.stdin.write(wire_bytes)
                self.process.stdin.flush()
        except Exception as exc:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            raise RuntimeError(f"Failed to write to LSP stdin: {exc}") from exc

        try:
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            if "error" in res:
                err = res["error"]
                err_msg = err.get("message", "Unknown LSP error") if isinstance(err, dict) else str(err)
                raise RuntimeError(f"LSP error for {method}: {err_msg}")
            return res.get("result")
        except asyncio.TimeoutError:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            # Try to send cancel request if timed out
            self.send_notification("$/cancelRequest", {"id": req_id})
            raise TimeoutError(f"LSP request '{method}' (id={req_id}) timed out after {timeout_s}s")

    def send_notification(self, method: str, params: Any = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self.is_alive():
            return

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params if params is not None else {},
        }
        wire_bytes = encode_lsp_message(payload)
        try:
            if self.process and self.process.stdin:
                self.process.stdin.write(wire_bytes)
                self.process.stdin.flush()
        except Exception:
            pass

    def add_notification_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._notification_handlers.append(handler)

    def _cleanup(self) -> None:
        self._closed = True
        with self._lock:
            for fut in self._pending_requests.values():
                if not fut.done() and self._loop:
                    self._loop.call_soon_threadsafe(
                        fut.set_exception, RuntimeError("LSP server process exited")
                    )
            self._pending_requests.clear()

    async def close(self, timeout_s: float = 3.0) -> None:
        """Gracefully close the LSP connection."""
        if self._closed:
            return
        self._closed = True

        try:
            if self.process and self.process.poll() is None:
                # Send exit notification if possible
                self.send_notification("exit")
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            pass
        finally:
            self._cleanup()
