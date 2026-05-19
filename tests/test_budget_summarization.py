"""Tests for budget enforcement during context summarization."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coderAI.context.context_controller import ContextController
from coderAI.system.cost import CostTracker
from coderAI.system.error_policy import BudgetExceededError


def _oversized_messages(count: int = 40) -> list:
    msgs = [{"role": "user", "content": "initial task"}]
    for i in range(count):
        msgs.append({"role": "user", "content": f"message {i} " + ("x" * 400)})
        msgs.append({"role": "assistant", "content": f"reply {i} " + ("y" * 400)})
    return msgs


@pytest.mark.asyncio
async def test_summarization_raises_when_budget_exceeded_before_llm_call():
    config = MagicMock()
    config.context_window = 2000
    config.default_model = "claude-sonnet-4-6"
    config.budget_limit = 1.0

    provider = MagicMock()
    provider.get_model_info.return_value = {"total_input_tokens": 0, "total_output_tokens": 0}
    provider.chat = AsyncMock()

    cost_tracker = CostTracker()
    cost_tracker.total_cost_usd = 2.0

    controller = ContextController(config=config, provider=provider, cost_tracker=cost_tracker)
    controller.estimate_tokens = MagicMock(return_value=5000)
    controller._estimate_message_tokens = MagicMock(return_value=50)

    with pytest.raises(BudgetExceededError):
        await controller.manage_context_window(_oversized_messages())

    provider.chat.assert_not_called()
