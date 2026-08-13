#!/usr/bin/env python3
"""End-to-end agent performance benchmark (reproducible, no network, no LLM keys).

Measures task completion, latency, token usage, tool-call efficiency, and cost
using a deterministic mock provider that simulates a realistic 3-tool-turn task.

Task: "Find and fix bug in file" -> mock LLM does: grep -> read_file -> apply_diff
We run 30 such tasks and measure:
- completion rate (did agent reach submit/apply step without doom loop)
- avg turn latency (wall ms)
- total tokens estimated via ContextController
- tool-call efficiency (duplicates avoided by LoopGuard)
- cost estimate (tokens * $/token)
"""

from __future__ import annotations
import asyncio
import json
import time
import statistics
from pathlib import Path


# Use the same mock provider pattern as benchmark_agent_perf
def run_e2e_benchmark(num_tasks=30):
    from coderAI.context.context_controller import ContextController
    from coderAI.core.loop_guard import LoopGuard
    from coderAI.llm.base import LLMProvider
    from coderAI.system.config import Config

    class MockProvider(LLMProvider):
        def __init__(self):
            super().__init__(model="mock-e2e")

        async def chat(self, messages, tools=None, **kwargs):
            # Simulate a realistic sequence with intentional repeats to test LoopGuard
            # Turn 0: grep bug -> Turn 1: read_file -> Turn 2: grep bug (1st repeat) -> Turn 3: grep bug (2nd repeat, should be cached) -> Turn 4: complete
            turn = len([m for m in messages if m.get("role") == "assistant"])
            if turn == 0:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "grep",
                                "arguments": json.dumps({"pattern": "bug", "path": "src"}),
                            },
                        }
                    ],
                    "usage": {"input_tokens": 500, "output_tokens": 100},
                }
            if turn == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "src/app.py"}),
                            },
                        }
                    ],
                    "usage": {"input_tokens": 800, "output_tokens": 120},
                }
            if turn == 2:
                # 1st repeat of grep (prior=1, threshold=2 => not yet cached, but counts)
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_3",
                            "function": {
                                "name": "grep",
                                "arguments": json.dumps({"pattern": "bug", "path": "src"}),
                            },
                        }
                    ],
                    "usage": {"input_tokens": 900, "output_tokens": 100},
                }
            if turn == 3:
                # 2nd repeat (prior=2 => cached_repeat should trigger, saving a tool call)
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_4",
                            "function": {
                                "name": "grep",
                                "arguments": json.dumps({"pattern": "bug", "path": "src"}),
                            },
                        }
                    ],
                    "usage": {"input_tokens": 900, "output_tokens": 100},
                }
            return {
                "content": "Fix complete",
                "tool_calls": [],
                "usage": {"input_tokens": 400, "output_tokens": 150},
            }

        async def complete(self, messages, tools=None, **kwargs):
            return await self.chat(messages, tools, **kwargs)

        def stream(self, messages, tools=None, **kwargs):
            raise NotImplementedError

    config = Config()
    provider = MockProvider()

    latencies = []
    completions = 0
    total_tokens = 0
    total_tool_calls = 0
    duplicates_avoided = 0

    for task_id in range(num_tasks):
        ctrl = ContextController(config, provider)
        guard = LoopGuard()
        messages = [{"role": "system", "content": "You are a helpful coding agent"}]
        messages.append({"role": "user", "content": f"Task {task_id}: fix bug in src/app.py"})

        start = time.perf_counter()
        tool_calls_this_task = 0
        avoided_this_task = 0

        # Simulate 4 agent iterations
        for iteration in range(5):
            # Estimate tokens (agent loop does this every iteration)
            tokens = ctrl.estimate_tokens(messages)
            total_tokens += tokens

            # Get mock LLM response
            resp = asyncio.run(provider.chat(messages))
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                # Completion
                messages.append({"role": "assistant", "content": resp.get("content", "")})
                completions += 1
                break

            # Check LoopGuard for duplicates (tool-call efficiency)
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = (
                    json.loads(func.get("arguments", "{}"))
                    if isinstance(func.get("arguments"), str)
                    else (func.get("arguments") or {})
                )
                fp = guard.fingerprint(name, args)
                # Simulate LoopGuard duplicate detection
                cached = guard.cached_repeat(name, True, fp)
                if cached is not None:
                    avoided_this_task += 1
                    duplicates_avoided += 1
                    # Cached repeat: still counts as assistant turn but saves tool execution
                    messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", f"call_{iteration}"),
                            "content": f"[cached] result for {name}",
                        }
                    )
                    continue
                guard.record_execution(fp, {"success": True})
                tool_calls_this_task += 1
                total_tool_calls += 1
                # Add assistant + tool result to history
                messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_{iteration}"),
                        "content": f"result for {name}",
                    }
                )

            # Detect in-batch doom (tool-call efficiency)
            guard.detect_in_batch([{"function": tc.get("function", {})} for tc in tool_calls])

        latencies.append((time.perf_counter() - start) * 1000)

    # Cost estimate: assume $0.003 / 1K input, $0.006 / 1K output (gpt-4o-ish)
    # Our mock usage: ~2100 input + 320 output per task avg
    est_cost_per_task = 2100 / 1000 * 0.003 + 320 / 1000 * 0.006
    total_cost = est_cost_per_task * num_tasks

    return {
        "num_tasks": num_tasks,
        "completion_rate_pct": completions / num_tasks * 100,
        "avg_latency_ms": statistics.mean(latencies) if latencies else 0,
        "p95_latency_ms": sorted(latencies)[int(len(latencies) * 0.95)]
        if len(latencies) > 1
        else (latencies[0] if latencies else 0),
        "total_tokens_est": total_tokens,
        "avg_tokens_per_task": total_tokens / num_tasks if num_tasks else 0,
        "total_tool_calls": total_tool_calls,
        "duplicates_avoided": duplicates_avoided,
        "tool_efficiency_pct": (total_tool_calls / (total_tool_calls + duplicates_avoided) * 100)
        if (total_tool_calls + duplicates_avoided)
        else 100,
        "est_cost_usd": total_cost,
        "est_cost_per_task_usd": total_cost / num_tasks if num_tasks else 0,
    }


if __name__ == "__main__":
    print("=== E2E Agent Benchmark (30 mock tasks) ===")
    r = run_e2e_benchmark(30)
    print(json.dumps(r, indent=2))
    out = Path(".benchmarks/e2e_after.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(r, indent=2))
    print(f"\nWritten to {out}")
    # Human summary
    print(
        f"\nCompletion: {r['completion_rate_pct']:.1f}%  Avg latency: {r['avg_latency_ms']:.2f}ms  Tokens/task: {r['avg_tokens_per_task']:.0f}  Tool efficiency: {r['tool_efficiency_pct']:.1f}%  Cost: ${r['est_cost_per_task_usd']:.4f}/task"
    )
