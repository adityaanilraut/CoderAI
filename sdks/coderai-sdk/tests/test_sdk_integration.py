"""Integration tests for the headless SDK against the real engine.

Gated behind ``CODERAI_SDK_INTEGRATION=1`` so the default SDK suite stays
offline (see ``test_sdk.py``). These tests construct a live
``SessionManagerEngine`` via ``build_session_manager`` but never start a
turn, so no LLM credentials or network access are required.

Run from the repo root:

    CODERAI_SDK_INTEGRATION=1 /usr/local/bin/python3 -m pytest \\
        sdks/coderai-sdk/tests/test_sdk_integration.py -p no:cacheprovider -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "src"),
)

REQUIRES_INTEGRATION = pytest.mark.skipif(
    os.getenv("CODERAI_SDK_INTEGRATION") != "1",
    reason="Set CODERAI_SDK_INTEGRATION=1 to run live-engine SDK tests",
)

try:
    from coderai_sdk import CoderAIClient

    _HAS_SDK = True
except ImportError:  # pragma: no cover - SDK src layout broken
    CoderAIClient = None  # type: ignore[assignment,misc]
    _HAS_SDK = False

try:
    from coderai.acp.engine import SessionManagerEngine
    from coderai.cli.session_factory import build_session_manager
    from coderai.soul.session.manager import SessionManager

    _HAS_ENGINE = True
except ImportError:  # pragma: no cover - engine not importable standalone
    SessionManagerEngine = None  # type: ignore[assignment,misc]
    build_session_manager = None  # type: ignore[assignment]
    SessionManager = None  # type: ignore[assignment,misc]
    _HAS_ENGINE = False

pytestmark = pytest.mark.skipif(
    not (_HAS_SDK and _HAS_ENGINE),
    reason="coderai engine or SDK source not importable",
)


@REQUIRES_INTEGRATION
def test_create_binds_real_session_manager(tmp_path) -> None:
    """``create()`` must wire the live Stack A engine (no LLM turn started)."""
    client = CoderAIClient.create(str(tmp_path))
    assert isinstance(client.engine, SessionManagerEngine)
    assert isinstance(client.engine.manager, SessionManager)
    assert client.session_id is None


@REQUIRES_INTEGRATION
def test_create_honours_model_plan_mode_and_skills(tmp_path) -> None:
    """Model/plan/skill options must reach the live manager/engine intact."""
    skills = ["tdd-workflow"]
    client = CoderAIClient.create(
        str(tmp_path), model="gpt-5.6-luna", plan_mode=True, skills=skills
    )
    assert client.engine.manager.get_active_model() == "gpt-5.6-luna"
    assert client.engine._plan_mode is True
    assert client.engine._skills == skills


@REQUIRES_INTEGRATION
def test_session_delegation_against_live_manager(tmp_path) -> None:
    """bind/set_model/interrupt must delegate to the real ``SessionManager``."""
    client = CoderAIClient.create(str(tmp_path))
    client.bind_session("sess-live-1")
    assert client.session_id == "sess-live-1"
    client.set_model("gpt-5.6-luna")
    assert client.engine.manager.get_active_model() == "gpt-5.6-luna"
    client.interrupt()  # no-op before the first turn; must not raise


@REQUIRES_INTEGRATION
def test_build_session_manager_accepts_sdk_surface(tmp_path) -> None:
    """Direct ``build_session_manager`` parity check for the SDK's kwargs."""
    manager = build_session_manager(str(tmp_path), model="gpt-5.6-luna", non_interactive=True)
    assert isinstance(manager, SessionManager)
    assert manager.non_interactive is True
    assert manager.get_active_model() == "gpt-5.6-luna"
