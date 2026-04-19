"""Smart context selection for CoderAI.

Provides relevance-based filtering so agents receive only the context
they need for the current task instead of everything available.
"""

import re
import logging
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "about", "up", "also", "this", "that", "these",
    "those", "it", "its", "i", "me", "my", "we", "our", "you", "your",
    "he", "him", "his", "she", "her", "they", "them", "their", "what",
    "which", "who", "whom", "make", "please", "help", "want", "need",
    "look", "like", "use", "get", "let", "put", "take", "give", "go",
    "come", "see", "know", "think", "say", "tell", "show", "try", "ask"
})

# Regex for block-starting statements across common languages
_BLOCK_START_RE = re.compile(
    r"^(def |class |async def |function |const |let |var |export |"
    r"public |private |protected |static |impl |fn |struct |enum |trait )"
)


def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text for relevance matching.

    Pulls out identifiers (camelCase, snake_case, PascalCase),
    file paths, and domain terms while filtering stop words.
    """
    keywords: List[str] = []

    # File paths / module references (e.g. src/auth/login.ts)
    for match in re.finditer(r"[\w./\\-]+\.[\w]+", text):
        keywords.append(match.group())

    # Identifiers: camelCase, snake_case, PascalCase
    for match in re.finditer(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", text):
        ident = match.group()
        lower = ident.lower()
        if lower in _STOP_WORDS or len(lower) < 2:
            continue
        keywords.append(lower)

        # Split compound identifiers into sub-words
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)", ident)
        for p in parts:
            pl = p.lower()
            if pl not in _STOP_WORDS and len(pl) > 1:
                keywords.append(pl)

    # Quoted strings often hold specific references
    for q in re.findall(r"""["\']([^"\']+)["\']""", text):
        keywords.extend(
            w.lower()
            for w in q.split()
            if w.lower() not in _STOP_WORDS and len(w) > 1
        )

    return keywords


def score_relevance(
    keywords: List[str], content: str, file_path: str = ""
) -> float:
    """Score how relevant *content* is to *keywords* (0.0 – 1.0)."""
    if not keywords or not content:
        return 0.0

    content_lower = content.lower()
    path_lower = file_path.lower()

    keyword_counts = Counter(keywords)
    total_weight = sum(keyword_counts.values())
    matched_weight = 0.0

    for kw, count in keyword_counts.items():
        if kw in path_lower:
            if len(kw) > 4:
                matched_weight += count * 4.0
            else:
                matched_weight += count * 2.0
        elif kw in content_lower:
            matched_weight += count * 1.0
            
    # Guarantee highly specific paths pass the threshold naturally
    raw = matched_weight / (total_weight * 1.5) if total_weight else 0.0
    return min(1.0, raw)


def extract_relevant_snippets(
    content: str,
    keywords: List[str],
    max_lines: int = 80,
    context_lines: int = 3,
) -> str:
    """Extract only the relevant functions/classes/blocks from file content.

    Instead of the full file, returns blocks that contain the keywords
    plus surrounding context.
    """
    lines = content.split("\n")
    if len(lines) <= max_lines:
        return content

    keywords_lower = {kw.lower() for kw in keywords}

    # Identify lines that match any keyword
    relevant_lines: Set[int] = set()
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(kw in line_lower for kw in keywords_lower):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            relevant_lines.update(range(start, end))

    if not relevant_lines:
        head = lines[: max_lines // 2]
        tail = lines[-(max_lines // 2) :]
        return (
            "\n".join(head)
            + f"\n\n... [{len(lines) - max_lines} lines omitted] ...\n\n"
            + "\n".join(tail)
        )

    # Locate block-starting lines (function/class definitions)
    block_starts: List[int] = [
        i for i, line in enumerate(lines) if _BLOCK_START_RE.match(line.lstrip())
    ]

    # Expand each relevant line to include its enclosing block
    expanded: Set[int] = set()
    for rel in sorted(relevant_lines):
        enclosing_start = 0
        for bs in block_starts:
            if bs <= rel:
                enclosing_start = bs
            else:
                break

        bs_idx = (
            block_starts.index(enclosing_start) if enclosing_start in block_starts else -1
        )
        if bs_idx >= 0 and bs_idx + 1 < len(block_starts):
            enclosing_end = block_starts[bs_idx + 1]
        else:
            enclosing_end = len(lines)

        enclosing_end = min(enclosing_end, enclosing_start + max_lines // 2)
        expanded.update(range(enclosing_start, enclosing_end))

    sorted_lines = sorted(expanded)
    if not sorted_lines:
        return content[: max_lines * 80]

    result_parts: List[str] = []
    prev_line = -2
    for line_num in sorted_lines:
        if line_num > prev_line + 1 and prev_line >= 0:
            gap = line_num - prev_line - 1
            result_parts.append(f"  ... [{gap} lines omitted] ...")
        result_parts.append(lines[line_num])
        prev_line = line_num

    remaining = len(lines) - prev_line - 1
    if remaining > 0:
        result_parts.append(f"  ... [{remaining} more lines] ...")

    result = "\n".join(result_parts)
    result_line_count = len(result.split("\n"))
    if result_line_count > max_lines * 2:
        result = "\n".join(result.split("\n")[: max_lines * 2]) + "\n... [truncated]"

    return result


def select_relevant_files(
    files: Dict[str, str],
    query: str,
    max_files: int = 5,
    min_score: float = 0.1,
) -> List[Tuple[str, str, float]]:
    """Return the most relevant files for *query*, sorted by relevance."""
    keywords = extract_keywords(query)
    if not keywords:
        return [(p, c, 0.5) for p, c in list(files.items())[:max_files]]

    scored: List[Tuple[str, str, float]] = []
    for path, content in files.items():
        s = score_relevance(keywords, content, file_path=path)
        if s >= min_score:
            scored.append((path, content, s))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:max_files]


def build_focused_context(
    files: Dict[str, str],
    query: str,
    project_instructions: Optional[str] = None,
    max_total_chars: int = 30_000,
    max_files: int = 5,
) -> Optional[str]:
    """Build a focused context string with only relevant files and snippets.

    Main entry point: given all available context files and the current query,
    returns a formatted context string containing only what matters.
    """
    keywords = extract_keywords(query)
    relevant = select_relevant_files(files, query, max_files=max_files)

    parts: List[str] = []
    total_chars = 0

    if project_instructions:
        parts.append("## Project Instructions\n" + project_instructions)
        total_chars += len(parts[-1])

    if not relevant:
        return "\n\n".join(parts) if parts else None

    file_parts: List[str] = []
    for path, content, score_val in relevant:
        snippet = extract_relevant_snippets(content, keywords)
        entry = f"### File: {path} (relevance: {score_val:.0%})\n```\n{snippet}\n```"

        if total_chars + len(entry) > max_total_chars:
            short = extract_relevant_snippets(content, keywords, max_lines=30)
            entry = f"### File: {path} (relevance: {score_val:.0%})\n```\n{short}\n```"
            if total_chars + len(entry) > max_total_chars:
                break

        file_parts.append(entry)
        total_chars += len(entry)

    if file_parts:
        parts.append(
            "## Relevant Context Files\n"
            "The following file snippets are selected based on relevance to your current task:\n\n"
            + "\n\n".join(file_parts)
        )

    return "\n\n".join(parts) if parts else None


def summarize_conversation_focus(
    messages: List[Dict[str, str]], recent_count: int = 6
) -> str:
    """Derive a short textual summary of the conversation's current focus.

    Used as the *query* when deciding which context files are relevant.
    """
    recent = messages[-recent_count:] if len(messages) > recent_count else messages
    parts: List[str] = []
    
    # Always include the very first user message to preserve the main task context
    if len(messages) > recent_count:
        first_msg = next((m for m in messages if m.get("role") == "user"), None)
        if first_msg and first_msg not in recent:
            if first_content := first_msg.get("content"):
                parts.append(first_content[:300])

    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role in ("user", "assistant") and content:
            parts.append(content[:300])
    return " ".join(parts)
