"""Coverage for coderAI/tui/session_setup.py — agent/bridge bootstrap."""

import contextlib
from unittest.mock import MagicMock, patch

from coderAI.core.agent import Agent
from coderAI.core.agent_tracker import AgentStatus
from coderAI.system.config import Config
from coderAI.tui.session_setup import (
    _activate_resumed_session_model,
    create_agent_session,
)


def _provider():
    p = MagicMock()
    p.count_tokens = lambda text: max(1, len(str(text)) // 4)
    return p


@contextlib.contextmanager
def _agent_env():
    """Patch config + provider so a real Agent can be built in-process."""
    with patch("coderAI.core.agent.config_manager") as cm:
        cfg = Config()
        cm.load.return_value = cfg
        cm.load_project_config.return_value = cfg
        with patch.object(Agent, "_create_provider", return_value=_provider()):
            yield


# ── _activate_resumed_session_model early return ────────────────────────


def test_activate_resumed_session_model_no_session():
    agent = MagicMock()
    agent.session = None
    _activate_resumed_session_model(agent, None)
    # Early return: no provider rebuild attempted.
    agent._create_provider.assert_not_called()


# ── create_agent_session ────────────────────────────────────────────────


def test_create_fresh_session():
    events = []
    with _agent_env():
        agent, controller = create_agent_session(on_event=lambda t, d: events.append((t, d)))
    assert agent.session is not None
    assert agent.ipc_server is controller
    assert agent.streaming_handler is not None
    assert agent.tracker_info.status == AgentStatus.IDLE
    assert agent.tracker_info is not None


def test_continue_resolves_latest_session():
    with _agent_env():
        with (
            # Latest-session lookup now lives in the shared cli.bootstrap module.
            patch("coderAI.core.session_bootstrap.history_manager") as hm,
            patch.object(Agent, "load_session", return_value=MagicMock(model="claude-sonnet-4-6")),
        ):
            hm.get_latest_session_id.return_value = "sess-123"
            agent, controller = create_agent_session(continue_=True, on_event=lambda *a: None)
            hm.get_latest_session_id.assert_called_once()
            Agent.load_session.assert_called_once_with("sess-123")
    assert controller is agent.ipc_server


def test_resume_load_failure_starts_fresh():
    events = []
    with _agent_env():
        with patch.object(Agent, "load_session", side_effect=RuntimeError("corrupt")):
            agent, _ = create_agent_session(
                resume="bad-id", on_event=lambda t, d: events.append((t, d))
            )
    # Failed resume falls back to a fresh session and warns via on_event.
    assert agent.session is not None
    assert any(t == "warning" for t, d in events)


def test_resume_load_failure_swallows_on_event_error():
    def bad_on_event(t, d):
        raise RuntimeError("event sink down")

    with _agent_env():
        with patch.object(Agent, "load_session", side_effect=RuntimeError("corrupt")):
            # A raising on_event during the resume-failure warning must not propagate.
            agent, _ = create_agent_session(resume="bad-id", on_event=bad_on_event)
    assert agent.session is not None


def test_resume_load_returns_none_starts_fresh():
    with _agent_env():
        with patch.object(Agent, "load_session", return_value=None):
            agent, _ = create_agent_session(resume="missing", on_event=lambda *a: None)
    assert agent.session is not None


def test_resume_success_activates_session_model():
    with _agent_env():
        with patch.object(Agent, "load_session", return_value=MagicMock(model="x")):
            agent, controller = create_agent_session(
                resume="good-id", model="gpt-5.4-mini", on_event=lambda *a: None
            )
    assert agent.ipc_server is controller
    assert agent.tracker_info.status == AgentStatus.IDLE
