"""Fuzzy matching and ranking heuristics for CoderAI autocompleters, file mentions, and menus."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def fuzzy_score(query: str, candidate: str) -> tuple[bool, int]:
    """Calculate whether query is a subsequence of candidate and compute a ranking score.

    Returns:
        tuple[bool, int]: (is_match, score)
    """
    if not query:
        return True, 0

    q_lower = query.lower()
    c_lower = candidate.lower()

    # Exact match bonus
    if q_lower == c_lower:
        return True, 10000

    # Prefix match bonus
    if c_lower.startswith(q_lower):
        return True, 5000 + (len(q_lower) * 20) - len(c_lower)

    # Substring match bonus
    if q_lower in c_lower:
        idx = c_lower.find(q_lower)
        score = 2000 - (idx * 10) + (len(q_lower) * 20) - len(c_lower)
        return True, score

    # Subsequence matching
    score = 0
    q_idx = 0
    q_len = len(q_lower)
    c_len = len(c_lower)
    prev_c_idx = -2

    for c_idx, char in enumerate(c_lower):
        if q_idx < q_len and char == q_lower[q_idx]:
            # Base match points
            score += 15

            # Consecutive character match bonus
            if c_idx == prev_c_idx + 1:
                score += 30

            # Word boundary bonus (start of word or following separator)
            if c_idx == 0:
                score += 50
            elif candidate[c_idx - 1] in ("/", "\\", "_", "-", ".", " ", ":"):
                score += 40
            elif candidate[c_idx].isupper() and not candidate[c_idx - 1].isupper():
                score += 35

            prev_c_idx = c_idx
            q_idx += 1

    if q_idx == q_len:
        # Full subsequence matched; apply slight penalty for length distance
        score -= c_len
        return True, score

    return False, 0


def fuzzy_filter(
    query: str,
    candidates: list[T],
    key_func: Callable[[T], str] | None = None,
    limit: int = 15,
) -> list[T]:
    """Filter and rank candidates using fuzzy matching score."""
    if not query:
        return candidates[:limit]

    scored: list[tuple[int, T]] = []
    for item in candidates:
        text = key_func(item) if key_func is not None else str(item)
        matched, score = fuzzy_score(query, text)
        if matched:
            scored.append((score, item))

    # Sort descending by score
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]
