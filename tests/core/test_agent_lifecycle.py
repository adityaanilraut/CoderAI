from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.core.agent import Agent


@pytest.mark.asyncio
async def test_close_is_idempotent_and_flushes_final_save() -> None:
    agent = Agent.__new__(Agent)
    agent._closed = False
    agent.save_session = MagicMock()
    agent._pending_saves = {object()}
    agent._save_executor = None
    agent._flush_pending_saves = MagicMock()
    agent.streaming_handler = None
    agent.provider = MagicMock()
    agent.provider.close = AsyncMock()
    agent.tracker_info = None
    agent._finish_tracker = MagicMock()
    agent._mcp_health_task = None

    await agent.close()
    await agent.close()

    agent.save_session.assert_called_once()
    agent._flush_pending_saves.assert_called_once()
    agent.provider.close.assert_awaited_once()
