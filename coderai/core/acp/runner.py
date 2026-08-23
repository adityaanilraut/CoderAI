"""Out-of-process ACP Subagent Runner.

Drives a child ACP agent in a spawned subprocess over the Agent Control Protocol (ndjson stdio).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from coderai.core.acp.protocol import AcpMessage, AcpNdjsonParser, PROTOCOL_VERSION

logger = logging.getLogger(__name__)


@dataclass
class AcpRunConfig:
    command: str
    args: list[str] = field(default_factory=list)
    cwd: str = "."
    permission_policy: str = "allow"  # "allow" | "reject"
    timeout_seconds: float = 120.0
    env: dict[str, str] | None = None


class AcpSubagentRunner:
    """Drives one isolated child session over ACP protocol in a subprocess."""

    def __init__(self, config: AcpRunConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[bytes] | None = None
        self._parser = AcpNdjsonParser()
        self._next_id = 1
        self._pending_requests: dict[str | int, asyncio.Future[AcpMessage]] = {}
        self._accumulated_text: list[str] = []
        self._reader_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._lock = threading.Lock()
        self.session_id: str | None = None

    def _start_process(self) -> None:
        run_env = os.environ.copy()
        if self.config.env:
            run_env.update(self.config.env)

        cmd = [self.config.command] + self.config.args
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.config.cwd,
            env=run_env,
        )

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name=f"acp-reader-{self.config.command}",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_loop(self) -> None:
        stdout = self.process.stdout if self.process else None
        if not stdout:
            return

        while not self._closed and self.process and self.process.poll() is None:
            try:
                line = stdout.readline()
                if not line:
                    break
                messages = self._parser.feed(line)
                for msg in messages:
                    self._handle_incoming_message(msg)
            except Exception as exc:
                logger.debug(f"ACP read error: {exc}")
                break

    def _handle_incoming_message(self, msg: AcpMessage) -> None:
        # 1. Response to a pending request
        if msg.id is not None and (msg.result is not None or msg.error is not None):
            with self._lock:
                fut = self._pending_requests.pop(msg.id, None)
            if fut and not fut.done() and self._loop:
                self._loop.call_soon_threadsafe(fut.set_result, msg)
            return

        # 2. Server request to client (e.g. session/request_permission)
        if msg.method == "session/request_permission" and msg.id is not None:
            self._handle_permission_request(msg)
            return

        # 3. Server notification (e.g. session/update)
        if msg.method == "session/update":
            params = msg.params or {}
            content = params.get("content") or {}
            text = content.get("text") or params.get("text", "")
            if text:
                self._accumulated_text.append(str(text))

    def _handle_permission_request(self, msg: AcpMessage) -> None:
        decision = "allow" if self.config.permission_policy == "allow" else "reject"
        response_msg = AcpMessage(
            jsonrpc="2.0",
            id=msg.id,
            result={"decision": decision, "reason": f"Auto-resolved by ACP runner policy: {decision}"},
        )
        self._write_message(response_msg)

    def _write_message(self, msg: AcpMessage) -> None:
        if not self.process or not self.process.stdin or self._closed:
            return
        try:
            self.process.stdin.write(msg.encode_ndjson())
            self.process.stdin.flush()
        except Exception:
            pass

    async def _send_request(self, method: str, params: dict[str, Any], timeout_s: float = 30.0) -> Any:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[AcpMessage] = loop.create_future()
            self._pending_requests[req_id] = fut

        msg = AcpMessage(jsonrpc="2.0", id=req_id, method=method, params=params)
        self._write_message(msg)

        try:
            resp = await asyncio.wait_for(fut, timeout=timeout_s)
            if resp.error:
                raise RuntimeError(f"ACP error on {method}: {resp.error}")
            return resp.result
        except asyncio.TimeoutError:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"ACP request '{method}' timed out after {timeout_s}s")

    async def execute(self, prompt: str) -> dict[str, Any]:
        """Execute a full task turn over ACP lifecycle."""
        start_t = time.time()
        try:
            self._start_process()

            # 1. Initialize
            init_res = await self._send_request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientInfo": {"name": "CoderAI", "version": "1.0"},
                    "capabilities": {"permission": True, "fs": True},
                },
                timeout_s=15.0,
            )

            # 2. Session Create
            session_res = await self._send_request(
                "session/create",
                {"cwd": self.config.cwd, "meta": {"prompt": prompt[:100]}},
                timeout_s=15.0,
            )
            self.session_id = session_res.get("sessionId", str(uuid.uuid4()))

            # 3. Session Prompt
            prompt_res = await self._send_request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": prompt},
                timeout_s=self.config.timeout_seconds,
            )

            elapsed = time.time() - start_t
            final_text = "".join(self._accumulated_text).strip()
            if not final_text and isinstance(prompt_res, dict):
                final_text = str(prompt_res.get("response") or prompt_res.get("text") or "")

            return {
                "ok": True,
                "status": "completed",
                "summary": final_text or "Task completed via ACP agent.",
                "duration_seconds": elapsed,
                "session_id": self.session_id,
            }
        except Exception as exc:
            elapsed = time.time() - start_t
            return {
                "ok": False,
                "status": "failed",
                "summary": f"ACP execution failed: {exc}",
                "error": str(exc),
                "duration_seconds": elapsed,
            }
        finally:
            await self.close()

    async def close(self) -> None:
        self._closed = True
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            pass
