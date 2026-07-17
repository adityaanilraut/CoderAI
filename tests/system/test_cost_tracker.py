"""Tests for CostTracker concurrency safety."""

import asyncio

import pytest

from coderAI.system.cost import CostTracker


@pytest.mark.parametrize(
    ("model", "input_price", "output_price"),
    [
        ("gpt-5.6", 5.0, 30.0),
        ("gpt-5.6-sol", 5.0, 30.0),
        ("claude-opus-4-8", 5.0, 25.0),
        ("claude-4.8-opus", 5.0, 25.0),
        ("claude-fable-5", 10.0, 50.0),
        ("claude-sonnet-5", 3.0, 15.0),
    ],
)
def test_current_model_pricing(model, input_price, output_price):
    assert CostTracker.get_model_pricing(model) == {
        "input": input_price,
        "output": output_price,
        "pricing_known": True,
    }


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
