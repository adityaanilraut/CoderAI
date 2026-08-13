#!/usr/bin/env python3
"""Reproducible agent performance benchmarks.

Measures latency, token efficiency, and tool-call overhead on the hot paths:
- LoopGuard fingerprint throughput
- ContextController token estimation (incremental + cold)
- Code chunker throughput
- Search grep throughput

Produces a JSON baseline for before/after comparison.
"""

from __future__ import annotations

import json
import time
import tempfile
import os
from pathlib import Path


# --- LoopGuard benchmark ---
def bench_loop_guard(num_calls=5000):
    from coderAI.core.loop_guard import LoopGuard

    guard = LoopGuard()
    args_variants = [
        {"pattern": "class Foo", "path": "src"},
        {"pattern": "def bar", "path": "coderAI/core/agent_loop.py"},
        {"file": "README.md", "content": "x" * 500},
        {"file": "big.py", "content": "y" * 5000},
    ]
    # Warmup
    for i in range(100):
        guard.fingerprint("grep", args_variants[i % len(args_variants)])
    start = time.perf_counter()
    for i in range(num_calls):
        fp = guard.fingerprint("grep", args_variants[i % len(args_variants)])
        guard.record_execution(fp, {"success": True, "output": "ok"})
        # also test cached_repeat path
        if i % 3 == 0:
            guard.cached_repeat("grep", True, fp)
    elapsed = time.perf_counter() - start
    ops = num_calls / elapsed
    return {"num_calls": num_calls, "elapsed_s": elapsed, "ops_per_sec": ops}


def bench_in_batch_detection():
    from coderAI.core.loop_guard import LoopGuard

    guard = LoopGuard()
    tool_calls = [
        {"function": {"name": "grep", "arguments": json.dumps({"pattern": "foo", "path": "src"})}}
        for _ in range(10)
    ]
    start = time.perf_counter()
    for _ in range(2000):
        guard.detect_in_batch(tool_calls)
    elapsed = time.perf_counter() - start
    return {"iterations": 2000, "elapsed_s": elapsed, "ops_per_sec": 2000 / elapsed}


# --- ContextController benchmark ---
def bench_context_controller():
    from coderAI.context.context_controller import ContextController
    from coderAI.llm.base import LLMProvider
    from coderAI.system.config import Config

    class FakeProvider(LLMProvider):
        def __init__(self):
            super().__init__(model="fake")

        async def complete(self, messages, tools=None, **kwargs):
            return {"content": "hi", "usage": {}}

        async def chat(self, messages, tools=None, **kwargs):
            return {"content": "hi", "usage": {}}

        def stream(self, messages, tools=None, **kwargs):
            raise NotImplementedError

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

    config = Config()
    provider = FakeProvider()
    ctrl = ContextController(config, provider)

    # Build realistic message history: system + 20 tool turns, each with large content
    base_messages = [{"role": "system", "content": "You are helpful. " * 50}]
    for i in range(20):
        base_messages.append({"role": "user", "content": f"Fix bug {i}"})
        base_messages.append(
            {
                "role": "assistant",
                "content": "I'll help",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": f"file{i}.py"}),
                        }
                    }
                ],
            }
        )
        base_messages.append(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "file content " * 500}
        )

    # Cold estimate
    start = time.perf_counter()
    for _ in range(50):
        ctrl.invalidate_token_cache()
        ctrl._token_cache.clear()
        ctrl.estimate_tokens(base_messages)
    cold_elapsed = time.perf_counter() - start

    # Incremental (hot path: agent loop appends 1 message each iteration)
    ctrl2 = ContextController(config, provider)
    # prime
    ctrl2.estimate_tokens(base_messages)
    msgs = list(base_messages)
    start = time.perf_counter()
    for i in range(100):
        msgs.append({"role": "assistant", "content": f"step {i} doing work"})
        ctrl2.estimate_tokens(msgs)
    incr_elapsed = time.perf_counter() - start

    # Fingerprint overhead isolated
    from coderAI.context.context_controller import ContextController as CC

    msg = {"role": "tool", "tool_call_id": "call_1", "content": "x" * 10000, "name": "read_file"}
    start = time.perf_counter()
    for _ in range(5000):
        CC._msg_fingerprint(msg)
    fp_elapsed = time.perf_counter() - start

    return {
        "cold_50x_elapsed_s": cold_elapsed,
        "cold_avg_ms": cold_elapsed / 50 * 1000,
        "incremental_100x_elapsed_s": incr_elapsed,
        "incremental_avg_ms": incr_elapsed / 100 * 1000,
        "fingerprint_5000x_elapsed_s": fp_elapsed,
        "fingerprint_ops_per_sec": 5000 / fp_elapsed,
    }


# --- Code chunker benchmark ---
def bench_code_chunker():
    from coderAI.context.code_chunker import chunk_file

    # Create temp python file with many functions
    code = "\n".join(
        [f"def func_{i}(x):\n    '''doc for {i}'''\n    return x + {i}\n" for i in range(100)]
    )
    code += "\n" + "x = 1\n" * 200
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir="/tmp") as f:
        f.write(code)
        path = f.name
    p = Path(path)
    root = p.parent
    try:
        start = time.perf_counter()
        for _ in range(200):
            chunk_file(p, root)
        elapsed = time.perf_counter() - start
        return {"iterations": 200, "elapsed_s": elapsed, "avg_ms": elapsed / 200 * 1000}
    finally:
        os.unlink(path)


def bench_code_chunker_large():
    from coderAI.context.code_chunker import chunk_file

    # Large non-python sliding-window path
    code = "hello world " * 10000
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, dir="/tmp") as f:
        f.write(code)
        path = f.name
    p = Path(path)
    root = p.parent
    try:
        start = time.perf_counter()
        for _ in range(100):
            chunk_file(p, root)
        elapsed = time.perf_counter() - start
        return {"iterations": 100, "elapsed_s": elapsed, "avg_ms": elapsed / 100 * 1000}
    finally:
        os.unlink(path)


# --- Grep tool benchmark (python fallback path) ---
def bench_grep_fallback():
    from coderAI.tools.search import GrepTool

    tool = GrepTool()
    # Create a tree with many files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for i in range(30):
            (tmp / f"file_{i}.py").write_text("class Foo:\n    pass\n" * 50 + f"# marker {i}\n")
        # Ensure resolve_under_project won't block; use actual project path search
        # We'll benchmark _python_grep directly
        start = time.perf_counter()
        for _ in range(20):
            tool._python_grep("class Foo", str(tmp), False, True, 50)
        elapsed = time.perf_counter() - start
        return {"iterations": 20, "elapsed_s": elapsed, "avg_ms": elapsed / 20 * 1000}


def main():
    print("=== CoderAI Agent Performance Baseline ===")
    results = {}
    print("\n[1/6] LoopGuard fingerprint...")
    r = bench_loop_guard()
    results["loop_guard"] = r
    print(f"  {r['ops_per_sec']:.0f} ops/sec ({r['elapsed_s']:.3f}s for {r['num_calls']} calls)")

    print("[2/6] LoopGuard in-batch detection...")
    r = bench_in_batch_detection()
    results["in_batch"] = r
    print(f"  {r['ops_per_sec']:.0f} ops/sec ({r['elapsed_s']:.3f}s)")

    print("[3/6] ContextController token estimation...")
    r = bench_context_controller()
    results["context_controller"] = r
    print(
        f"  cold avg {r['cold_avg_ms']:.2f} ms, incremental avg {r['incremental_avg_ms']:.2f} ms, fingerprint {r['fingerprint_ops_per_sec']:.0f} ops/sec"
    )

    print("[4/6] Code chunker (python)...")
    r = bench_code_chunker()
    results["chunker_py"] = r
    print(f"  avg {r['avg_ms']:.2f} ms ({r['elapsed_s']:.3f}s for {r['iterations']} iter)")

    print("[5/6] Code chunker (sliding window)...")
    r = bench_code_chunker_large()
    results["chunker_txt"] = r
    print(f"  avg {r['avg_ms']:.2f} ms")

    print("[6/6] Grep python fallback...")
    # needs mock project root; skip if resolve fails
    try:
        r = bench_grep_fallback()
        results["grep"] = r
        print(f"  avg {r['avg_ms']:.2f} ms")
    except Exception as e:
        print(f"  skipped: {e}")
        results["grep"] = {"error": str(e)}

    # Also measure overall test suite timing for tool-call efficiency? We'll use pytest timing from earlier
    print("\n=== Summary ===")
    print(json.dumps(results, indent=2))
    out = Path(".benchmarks/baseline.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nBaseline written to {out}")

    # Write human readable
    with open(".benchmarks/baseline.txt", "w") as f:
        f.write("CoderAI Baseline Benchmarks\n")
        f.write("===========================\n")
        f.write(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
