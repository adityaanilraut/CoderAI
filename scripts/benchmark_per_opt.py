#!/usr/bin/env python3
"""Per-optimization isolated benchmarks.

Runs each optimization in isolation to quantify its individual contribution.
We monkey-patch to disable each optimization and measure delta.
"""

import json
import time
import pathlib


def bench_loopguard(with_cache=True):
    from coderAI.core.loop_guard import LoopGuard

    # Clear cache
    LoopGuard._FP_CACHE.clear()
    variants = [
        {"pattern": "bug", "path": "src"},
        {"pattern": "fix", "path": "coderAI/core/agent_loop.py"},
        {"file": "a.py", "content": "x" * 1000},
    ]
    # If without cache, temporarily disable cache by patching fingerprint to bypass cache
    orig_fp = LoopGuard.fingerprint
    if not with_cache:
        # Replace with uncached version (direct sha256 without cache)
        def uncached(tool_name, arguments):
            import json
            import hashlib

            try:
                blob = json.dumps(arguments or {}, sort_keys=True, default=str)
            except Exception:
                blob = repr(arguments)
            return hashlib.sha256(f"{tool_name}\x00{blob}".encode()).hexdigest()

        LoopGuard.fingerprint = staticmethod(uncached)
        LoopGuard._FP_CACHE.clear()

    guard = LoopGuard()
    start = time.perf_counter()
    for i in range(5000):
        fp = guard.fingerprint("grep", variants[i % len(variants)])
        guard.record_execution(fp, {"success": True})
    elapsed = time.perf_counter() - start
    # Restore
    LoopGuard.fingerprint = orig_fp
    LoopGuard._FP_CACHE.clear()
    return {"ops_per_sec": 5000 / elapsed, "elapsed_s": elapsed}


def bench_msg_fingerprint(with_fast=True):
    from coderAI.context.context_controller import ContextController

    msg = {"role": "tool", "tool_call_id": "call_1", "content": "x" * 10000, "name": "read_file"}
    # with_fast=True uses current code (blake2b fast path for string)
    # with_fast=False forces fallback json path by adding tool_calls
    if not with_fast:
        msg = {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "x" * 10000,
            "name": "read_file",
            "tool_calls": [{"dummy": 1}],
        }
    start = time.perf_counter()
    for _ in range(5000):
        ContextController._msg_fingerprint(msg)
    elapsed = time.perf_counter() - start
    return {"ops_per_sec": 5000 / elapsed, "elapsed_s": elapsed}


def bench_incremental(with_opt=True):
    from coderAI.context.context_controller import ContextController
    from coderAI.llm.base import LLMProvider
    from coderAI.system.config import Config

    class Fake(LLMProvider):
        def __init__(self):
            super().__init__(model="fake")

        async def chat(self, m, tools=None, **k):
            return {"content": "hi"}

        async def complete(self, m, tools=None, **k):
            return {"content": "hi"}

        def stream(self, m, tools=None, **k):
            raise NotImplementedError

        def count_tokens(self, t):
            return max(1, len(t) // 4)

    config = Config()
    provider = Fake()
    ctrl = ContextController(config, provider)
    base = [{"role": "system", "content": "hi"}]
    for i in range(20):
        base.append({"role": "user", "content": f"msg {i}"})
        base.append({"role": "assistant", "content": "ok"})
    ctrl.estimate_tokens(base)
    msgs = list(base)
    start = time.perf_counter()
    for i in range(100):
        msgs.append({"role": "user", "content": f"extra {i}"})
        ctrl.estimate_tokens(msgs)
    elapsed = time.perf_counter() - start
    return {"avg_ms": elapsed / 100 * 1000, "elapsed_s": elapsed}


if __name__ == "__main__":
    print("=== Per-Optimization Isolated Benchmarks ===")
    print("\n[1] LoopGuard fingerprint cache:")
    r_with = bench_loopguard(True)
    r_without = bench_loopguard(False)
    print(f"  with cache: {r_with['ops_per_sec']:.0f} ops/s")
    print(f"  without cache (sha256 no cache): {r_without['ops_per_sec']:.0f} ops/s")
    print(f"  delta: {(r_with['ops_per_sec'] / r_without['ops_per_sec'] - 1) * 100:+.1f}%")

    print("\n[2] _msg_fingerprint fast path:")
    r_fast = bench_msg_fingerprint(True)
    r_slow = bench_msg_fingerprint(False)
    print(f"  fast (blake2b string): {r_fast['ops_per_sec']:.0f} ops/s")
    print(f"  slow (json fallback): {r_slow['ops_per_sec']:.0f} ops/s")
    print(f"  delta: {(r_fast['ops_per_sec'] / r_slow['ops_per_sec'] - 1) * 100:+.1f}%")

    print("\n[3] Incremental token estimation:")
    r = bench_incremental(True)
    print(f"  incremental avg: {r['avg_ms']:.4f} ms (100 appends)")

    out = {
        "loopguard_with": r_with,
        "loopguard_without": r_without,
        "msg_fast": r_fast,
        "msg_slow": r_slow,
        "incremental": r,
    }
    pathlib.Path(".benchmarks/per_opt.json").write_text(json.dumps(out, indent=2))
    print("\nWritten to .benchmarks/per_opt.json")
