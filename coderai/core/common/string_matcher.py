"""Multi-stage string matching with deterministic fallbacks.

Mirroring deepseek-harness-master fsio.ts & tool-str-replace-editor:
Provides multi-stage search for file edits:
1. Exact literal matching (LF-normalized)
2. Tab-stripped / line-number stripped matching (for snippets copied from view/read outputs)
3. Quote and escape invariant matching (typographic quotes, double-escaping)
4. Line-by-line whitespace/trimmed matching (indentation-tolerant)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MatchResult:
    matches: list[tuple[int, int]]
    matched_via: str
    matched_text: str
    replaced_old: str
    replaced_new: str


def normalize_line_endings(val: str) -> str:
    """Normalize CRLF and CR to LF."""
    return val.replace("\r\n", "\n").replace("\r", "\n")


def find_occurrences(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Find all start and end offset occurrences of needle in haystack."""
    if not haystack or not needle:
        return []
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        pos = haystack.find(needle, idx)
        if pos == -1:
            break
        matches.append((pos, pos + len(needle)))
        idx = pos + len(needle)
    return matches


def strip_read_result_line_tabs(value: str) -> str:
    """Strip line numbers and tabs often copied from cat -n or read output."""
    v = re.sub(r"^\s*\d+\t", "", value)
    v = re.sub(r"\n\s*\d+\t", "\n", v)
    v = re.sub(r"\n\t", "\n", v)
    return v


def normalize_quotes(val: str) -> str:
    """Normalize curly / typographic quotes to standard ASCII quotes."""
    return (
        val.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
        .replace("`", "'")
    )


def normalize_escaping(val: str) -> str:
    """Normalize backslash escaping (e.g. \\{{ -> \\{{ or \\" -> ")."""
    return re.sub(r"\\+(?=[\"'`\\“”‘’{}()])", "", val)


def normalize_loose_text(val: str) -> str:
    v = normalize_line_endings(val)
    v = normalize_quotes(v)
    v = normalize_escaping(v)
    v = re.sub(r"[ \t]+", " ", v)
    return v.strip()


def find_trimmed_line_match(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Match needle lines to haystack lines by ignoring leading and trailing whitespace."""
    norm_haystack = normalize_line_endings(haystack)
    norm_needle = normalize_line_endings(needle).strip("\n")
    if not norm_needle or not norm_haystack:
        return []

    h_lines = norm_haystack.splitlines(keepends=True)
    n_lines = norm_needle.splitlines()

    if not n_lines:
        return []

    n_stripped = [line.strip() for line in n_lines]
    if not any(n_stripped):
        return []

    matches: list[tuple[int, int]] = []
    n_len = len(n_lines)

    # Compute line start offsets in haystack
    line_offsets: list[int] = [0]
    for line in h_lines:
        line_offsets.append(line_offsets[-1] + len(line))

    for i in range(len(h_lines) - n_len + 1):
        candidate_stripped = [h_lines[i + k].strip() for k in range(n_len)]
        if candidate_stripped == n_stripped:
            start_off = line_offsets[i]
            end_off = line_offsets[i + n_len - 1] + len(h_lines[i + n_len - 1].rstrip("\r\n"))
            matches.append((start_off, end_off))

    return matches


def find_quote_escape_matches(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Match needle against haystack when escaping or quotation marks vary."""
    norm_haystack = normalize_line_endings(haystack)
    norm_needle = normalize_line_endings(needle)
    if not norm_needle or not norm_haystack:
        return []

    # Try matching with quotes normalized
    q_haystack = normalize_quotes(norm_haystack)
    q_needle = normalize_quotes(norm_needle)
    if q_needle != norm_needle or q_haystack != norm_haystack:
        q_matches = find_occurrences(q_haystack, q_needle)
        if q_matches:
            return q_matches

    # Try matching with slashes and quotes normalized line by line
    h_lines = norm_haystack.splitlines(keepends=True)
    n_lines = norm_needle.splitlines()
    if not n_lines:
        return []

    n_loose = [normalize_loose_text(line) for line in n_lines]
    if not any(n_loose):
        return []

    n_len = len(n_lines)
    line_offsets: list[int] = [0]
    for line in h_lines:
        line_offsets.append(line_offsets[-1] + len(line))

    matches: list[tuple[int, int]] = []
    for i in range(len(h_lines) - n_len + 1):
        candidate_loose = [normalize_loose_text(h_lines[i + k]) for k in range(n_len)]
        if candidate_loose == n_loose:
            start_off = line_offsets[i]
            end_off = line_offsets[i + n_len - 1] + len(h_lines[i + n_len - 1].rstrip("\r\n"))
            matches.append((start_off, end_off))

    return matches


def match_multistage(
    haystack: str,
    needle: str,
    new_text: str = "",
) -> MatchResult:
    """Execute 4-stage deterministic string matching.

    Returns MatchResult with offsets, matched stage name, and adjusted old/new strings.
    """
    if not needle:
        return MatchResult(
            matches=[(0, 0)] if not haystack else [],
            matched_via="empty",
            matched_text="",
            replaced_old="",
            replaced_new=new_text,
        )

    norm_haystack = normalize_line_endings(haystack)
    norm_needle = normalize_line_endings(needle)
    norm_new = normalize_line_endings(new_text)

    # 1. Exact literal match
    matches = find_occurrences(norm_haystack, norm_needle)
    if matches:
        return MatchResult(
            matches=matches,
            matched_via="exact",
            matched_text=norm_haystack[matches[0][0] : matches[0][1]],
            replaced_old=norm_needle,
            replaced_new=norm_new,
        )

    # 2. Line leading tab / line numbers stripped
    tab_stripped = strip_read_result_line_tabs(norm_needle)
    if tab_stripped != norm_needle:
        tab_matches = find_occurrences(norm_haystack, tab_stripped)
        if tab_matches:
            return MatchResult(
                matches=tab_matches,
                matched_via="line_leading_tab_correction",
                matched_text=norm_haystack[tab_matches[0][0] : tab_matches[0][1]],
                replaced_old=tab_stripped,
                replaced_new=strip_read_result_line_tabs(norm_new),
            )

    # 3. Quote & escape normalization
    esc_matches = find_quote_escape_matches(norm_haystack, norm_needle)
    if len(esc_matches) == 1:
        s, e = esc_matches[0]
        actual_old = norm_haystack[s:e]
        return MatchResult(
            matches=esc_matches,
            matched_via="quote_escape_normalization",
            matched_text=actual_old,
            replaced_old=actual_old,
            replaced_new=norm_new,
        )

    # 4. Trimmed line-by-line match
    trimmed_matches = find_trimmed_line_match(norm_haystack, norm_needle)
    if len(trimmed_matches) == 1:
        s, e = trimmed_matches[0]
        actual_old = norm_haystack[s:e]
        return MatchResult(
            matches=trimmed_matches,
            matched_via="trimmed_line_match",
            matched_text=actual_old,
            replaced_old=actual_old,
            replaced_new=norm_new,
        )

    return MatchResult(
        matches=[],
        matched_via="not_found",
        matched_text="",
        replaced_old=norm_needle,
        replaced_new=norm_new,
    )
