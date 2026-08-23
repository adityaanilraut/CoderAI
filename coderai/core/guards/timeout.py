"""Cooperative Tool Timeout Policy and Deadline Guard.

Port of DeepSeek Harness dsh-guard/timeout-policy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from collections.abc import Callable

from coderai.core.tools.types import ToolResult

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_ERROR_CODE = "TOOL_TIMEOUT"


async def execute_with_timeout_policy(
    coro_or_func: Callable[[], Any],
    tool_name: str,
    timeout_seconds: float | None = None,
) -> ToolResult:
    """Execute a tool handler under cooperative timeout deadline."""
    if not timeout_seconds or timeout_seconds <= 0:
        res = coro_or_func()
        if asyncio.iscoroutine(res):
            return await res
        return res

    try:
        async def _wrapper() -> Any:
            res = coro_or_func()
            if asyncio.iscoroutine(res):
                return await res
            return res

        return await asyncio.wait_for(_wrapper(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return ToolResult(
            ok=False,
            name=tool_name,
            error=f"{TOOL_TIMEOUT_ERROR_CODE}: tool execution timed out after {timeout_seconds:.1f}s.",
            metadata={"code": TOOL_TIMEOUT_ERROR_CODE, "timeoutSeconds": timeout_seconds},
        )
