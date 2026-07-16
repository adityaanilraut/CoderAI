"""Tests for CostTracker concurrency safety."""

import asyncio

import pytest

from coderAI.system.cost import CostTracker


@pytest.mark.asyncio
async def test_add_cost_is_safe_under_concurrent_updates():
    tracker = CostTracker()
    model = "claude-sonnet-4-6"

    await asyncio.gather(
        *[tracker.add_cost(model, 1000, 500) for _ in range(20)],
    )

    assert tracker.get_total_cost() > 0
    expected = 0.0
    for _ in range(20):
        expected += tracker.calculate_cost_for_tokens(model, 1000, 500)
    assert tracker.get_total_cost() == pytest.approx(expected)
