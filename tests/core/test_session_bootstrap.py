from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from coderAI.core.session_bootstrap import bootstrap_agent


@pytest.mark.parametrize(
    ("requested_model", "expected_model"),
    [(None, "saved-model"), ("override-model", "override-model")],
)
def test_resume_activates_effective_model_and_refreshes_context(
    requested_model, expected_model
) -> None:
    session = SimpleNamespace(model="saved-model")
    agent = SimpleNamespace(
        model=requested_model or "default-model",
        session=None,
        load_session=MagicMock(return_value=session),
        create_session=MagicMock(),
        _replace_provider=MagicMock(),
        _rebuild_tool_registry=MagicMock(),
        _refresh_session_system_prompt=MagicMock(),
        _configure_delegate_tool_context=MagicMock(),
    )

    with patch("coderAI.core.session_bootstrap.Agent", return_value=agent):
        result = bootstrap_agent(model=requested_model, resume_id="session-1")

    assert result is agent
    assert agent.model == expected_model
    assert session.model == expected_model
    agent._rebuild_tool_registry.assert_called_once()
    agent._refresh_session_system_prompt.assert_called_once()
    agent._configure_delegate_tool_context.assert_called_once()
    if expected_model == (requested_model or "default-model"):
        agent._replace_provider.assert_not_called()
    else:
        agent._replace_provider.assert_called_once()
