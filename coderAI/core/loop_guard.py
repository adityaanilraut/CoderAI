"""Shared doom-loop / repeat detection for the execution loop (Phase 2.2).

Consolidates what used to be two separate implementations with two thresholds
and two message templates:

* in-batch repeat detection (was ``ExecutionLoop._detect_doom_loop``), and
* cross-iteration counting + cached-repeat short-circuit (was inlined in
  ``ToolExecutor._orchestrate_tool_calls``).

One :class:`LoopGuard` instance is created per user turn by ``ExecutionLoop``
and shared with its ``ToolExecutor``, so both layers agree on fingerprints,
thresholds, and the user-facing stop message.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# In-batch: the same (tool, args) emitted this many times within a SINGLE
# assistant response is treated as a stuck model. Matches OpenCode's default.
IN_BATCH_DOOM_THRESHOLD = 3

# Cross-iteration: identical (tool, args) may repeat this many times before the
# executor short-circuits a read-only / delegate call with a cached result.
DUPLICATE_CALL_THRESHOLD = 2

# Cross-iteration hard ceiling: once a fingerprint reaches this count the guard
# signals a full stop. Applies to ALL tools (read-only or not) — a mutating
# tool called identically N times almost always indicates a stuck model rather
# than legitimate work. Triggered in production by gpt-5.4-mini calling
# ``plan action=show`` 14+ times in a single turn before the user cancelled.
DOOM_LOOP_HARD_THRESHOLD = 10
DELEGATE_DOOM_LOOP_HARD_THRESHOLD = 3


def doom_message(tool_name: str, count: int) -> str:
    """The single user-facing stop message shared by the in-batch and
    cross-iteration doom paths, so both read identically."""
    return (
        f"Stopped to avoid wasting tokens: '{tool_name}' was called {count} "
        "times with identical arguments. The model appears to be looping. "
        "Please rephrase your request or provide additional guidance."
    )


class LoopGuard:
    """Per-turn fingerprint counter + doom-loop / cached-repeat detector."""

    def __init__(self) -> None:
        # fingerprint -> cumulative execution count this turn.
        self._call_counts: Dict[str, int] = {}
        # fingerprint -> last successful result, for the cached-repeat path.
        self._last_results: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def fingerprint(tool_name: str, arguments: Optional[Dict[str, Any]]) -> str:
        """Stable hash of ``(tool_name, arguments)`` used to spot repeats."""
        try:
            args_blob = json.dumps(arguments or {}, sort_keys=True, default=str)
        except Exception:
            args_blob = repr(arguments)
        return hashlib.sha256(f"{tool_name}\x00{args_blob}".encode("utf-8")).hexdigest()

    @staticmethod
    def hard_threshold(tool_name: str) -> int:
        """Cross-iteration hard stop threshold for *tool_name*."""
        if tool_name == "delegate_task":
            return DELEGATE_DOOM_LOOP_HARD_THRESHOLD
        return DOOM_LOOP_HARD_THRESHOLD

    def prior_count(self, fp: str) -> int:
        return self._call_counts.get(fp, 0)

    def cached_repeat(
        self, tool_name: str, is_read_only: bool, fp: str
    ) -> Optional[Tuple[Dict[str, Any], int]]:
        """Decide whether *this* repeat should be short-circuited with the
        previously cached result.

        Returns ``(cached_result_copy, repeated_count)`` when the call is a
        read-only / delegate repeat past its threshold with a cacheable prior
        success — recording the repeat against the counter as a side effect.
        Returns ``None`` otherwise (the call must actually run).
        """
        prior = self._call_counts.get(fp, 0)
        cached = self._last_results.get(fp)
        cacheable = isinstance(cached, dict) and cached.get("success") is True
        threshold = 1 if tool_name == "delegate_task" else DUPLICATE_CALL_THRESHOLD
        if not (
            (is_read_only or tool_name == "delegate_task") and prior >= threshold and cacheable
        ):
            return None
        repeated = prior + 1
        self._call_counts[fp] = repeated
        assert isinstance(cached, dict)  # narrowed by ``cacheable`` above
        return dict(cached), repeated

    def record_execution(self, fp: str, result: Any) -> int:
        """Count one executed (non-denied) call, caching a successful result.

        Returns the new cumulative count for this fingerprint.
        """
        count = self._call_counts.get(fp, 0) + 1
        self._call_counts[fp] = count
        if isinstance(result, dict) and result.get("success") is True:
            self._last_results[fp] = result
        return count

    def is_doom(self, tool_name: str, count: int) -> bool:
        """True when *count* has reached the hard stop threshold for *tool_name*."""
        return count >= self.hard_threshold(tool_name)

    def detect_in_batch(
        self, tool_calls: Optional[List[Dict[str, Any]]]
    ) -> Optional[Tuple[str, int]]:
        """Detect repetition *within a single* assistant response.

        Returns ``(tool_name, count)`` for the most-repeated (tool, args) when
        it appears at least :data:`IN_BATCH_DOOM_THRESHOLD` times, else ``None``.
        This mirrors OpenCode's doom-loop detection, which inspects parts within
        the same assistant message rather than across iterations.
        """
        if not tool_calls or len(tool_calls) < IN_BATCH_DOOM_THRESHOLD:
            return None

        from coderAI.core.tool_routing import coerce_tool_arguments

        counts: Dict[str, int] = {}
        names: Dict[str, str] = {}
        for tc in tool_calls:
            func = tc.get("function", {}) or {}
            name = func.get("name", "") or ""
            args, _ = coerce_tool_arguments(func.get("arguments"))
            fp = self.fingerprint(name, args)
            counts[fp] = counts.get(fp, 0) + 1
            names[fp] = name

        top_fp = max(counts, key=lambda k: counts[k])
        max_count = counts[top_fp]
        if max_count >= IN_BATCH_DOOM_THRESHOLD:
            logger.warning(
                "Doom loop detected: tool called %d times within a single LLM response",
                max_count,
            )
            return names[top_fp], max_count
        return None
