"""Splits source files into semantic chunks for embedding.

Strategy (best-effort):
- Python: AST-aware — split at top-level function / class / async-function boundaries.
- JS / TS / JSX / TSX: regex-aware — split on function, class, and export declarations.
- Everything else: sliding window with overlap.

Each chunk carries metadata so the search tool can report file + line range.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Files whose content is mostly structure, not meaning — skip them.
_SKIP_SUFFIXES: set[str] = {
    ".lock", ".min.js", ".min.css", ".map", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".xml", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".mp3", ".mp4", ".mov",
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".dat",
    ".db", ".sqlite", ".sqlite3",
}

_SKIP_DIRS: set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
    "build", ".next", ".nuxt", "target", ".tox", ".eggs", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "vendor", "bower_components",
    ".coderAI", ".claude",
}

# File suffixes we attempt to chunk.
_CODE_SUFFIXES: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".r": "r",
    ".scala": "scala",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    ".hs": "haskell",
    ".lhs": "haskell",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "restructuredtext",
    ".txt": "text",
}

_WINDOW_SIZE = 1000
_WINDOW_OVERLAP = 200


@dataclass
class Chunk:
    """A single chunk of source text with location metadata."""

    text: str
    file_path: str
    start_line: int
    end_line: int
    language: str
    chunk_type: str = "generic"  # function, class, method, module, generic


@dataclass
class ChunkResult:
    """All chunks extracted from a single file, plus a content hash."""

    chunks: list[Chunk] = field(default_factory=list)
    file_hash: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_file(file_path: Path, project_root: Path) -> ChunkResult:
    """Split *file_path* into chunks, returning them with a content hash."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ChunkResult()

    import hashlib

    file_hash = hashlib.sha256(text.encode()).hexdigest()
    rel = str(file_path.relative_to(project_root))

    suffix = file_path.suffix.lower()
    language = _CODE_SUFFIXES.get(suffix, "unknown")

    if suffix == ".py" or suffix == ".pyi":
        chunks = _chunk_python(text, rel, language)
    elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        chunks = _chunk_jsts(text, rel, language)
    else:
        chunks = _chunk_generic(text, rel, language)

    return ChunkResult(chunks=chunks, file_hash=file_hash)


def should_index(file_path: Path) -> bool:
    """Return True if *file_path* looks like a chunkable source file."""
    suffix = file_path.suffix.lower()
    if suffix in _SKIP_SUFFIXES:
        return False
    parts = set(file_path.parts)
    if parts & _SKIP_DIRS:
        return False
    if suffix in _CODE_SUFFIXES:
        return True
    return False


def is_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS


# ---------------------------------------------------------------------------
# Python — AST-aware
# ---------------------------------------------------------------------------


def _chunk_python(source: str, rel_path: str, language: str) -> list[Chunk]:
    """AST-based chunking: each top-level class / function becomes a chunk.

    Module-level statements (imports, assignments) are collected into a
    preamble chunk. Methods are kept inside their parent class chunk rather
    than being split out.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _chunk_generic(source, rel_path, language)

    lines = source.splitlines()
    chunks: list[Chunk] = []
    preamble_end = 0

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno or start
            body = "\n".join(lines[start - 1 : end])
            chunks.append(
                Chunk(
                    text=body,
                    file_path=rel_path,
                    start_line=start,
                    end_line=end,
                    language=language,
                    chunk_type=_func_type(node.name),
                )
            )
            preamble_end = max(preamble_end, end)

        elif isinstance(node, ast.ClassDef):
            start = node.lineno
            end = node.end_lineno or start
            body = "\n".join(lines[start - 1 : end])
            chunks.append(
                Chunk(
                    text=body,
                    file_path=rel_path,
                    start_line=start,
                    end_line=end,
                    language=language,
                    chunk_type="class",
                )
            )
            preamble_end = max(preamble_end, end)

        else:
            preamble_end = max(preamble_end, node.end_lineno or node.lineno)

    # Preamble chunk: everything before the first class/function, or the
    # whole file when there are no class/function-level nodes.
    if chunks and preamble_end > 0:
        preamble = "\n".join(lines[:preamble_end]).strip()
        if preamble:
            # Find where the preamble actually ends (last non-empty line
            # before the first entity)
            chunks.insert(
                0,
                Chunk(
                    text=preamble,
                    file_path=rel_path,
                    start_line=1,
                    end_line=preamble_end,
                    language=language,
                    chunk_type="module",
                ),
            )
    elif not chunks:
        chunks = _chunk_generic(source, rel_path, language)

    return _merge_short_chunks(chunks)


def _func_type(name: str) -> str:
    """Heuristic to label a function as a method vs standalone function."""
    if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
        return "function"  # private module-level helper
    if name.startswith("__") and name.endswith("__"):
        return "function"  # dunder
    return "function"


# ---------------------------------------------------------------------------
# JS / TS — regex-aware
# ---------------------------------------------------------------------------

_JS_PATTERNS: list[tuple[str, str]] = [
    # (regex, chunk_type)
    (r"export\s+(?:async\s+)?function\s+(\w+)", "function"),
    (r"export\s+(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "function"),
    (r"export\s+(?:default\s+)?class\s+(\w+)", "class"),
    (r"(?:async\s+)?function\s+(\w+)", "function"),
    (r"class\s+(\w+)", "class"),
]


def _chunk_jsts(source: str, rel_path: str, language: str) -> list[Chunk]:
    """Regex-based chunking for JavaScript-family languages."""
    lines = source.splitlines()

    # Collect candidate boundaries
    boundaries: list[tuple[int, str, str]] = []  # (line_no, name, chunk_type)
    for pattern, chunk_type in _JS_PATTERNS:
        for m in re.finditer(pattern, source, re.MULTILINE):
            line_no = source[: m.start()].count("\n") + 1
            name = m.group(1) if m.lastindex and m.lastindex >= 1 else "anonymous"
            boundaries.append((line_no, name, chunk_type))

    if not boundaries:
        return _chunk_generic(source, rel_path, language)

    boundaries.sort()
    # Deduplicate by line
    seen: set[int] = set()
    unique: list[tuple[int, str, str]] = []
    for b in boundaries:
        if b[0] not in seen:
            seen.add(b[0])
            unique.append(b)
    boundaries = unique

    chunks: list[Chunk] = []

    # Preamble
    if boundaries[0][0] > 1:
        pre = "\n".join(lines[: boundaries[0][0] - 1]).strip()
        if pre:
            chunks.append(
                Chunk(
                    text=pre,
                    file_path=rel_path,
                    start_line=1,
                    end_line=boundaries[0][0] - 1,
                    language=language,
                    chunk_type="module",
                )
            )

    # Entity chunks
    for i, (start_line, name, chunk_type) in enumerate(boundaries):
        end_line = boundaries[i + 1][0] - 1 if i + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[start_line - 1 : end_line]).strip()
        if body:
            chunks.append(
                Chunk(
                    text=body,
                    file_path=rel_path,
                    start_line=start_line,
                    end_line=end_line,
                    language=language,
                    chunk_type=chunk_type,
                )
            )

    return _merge_short_chunks(chunks)


# ---------------------------------------------------------------------------
# Generic — sliding window
# ---------------------------------------------------------------------------


def _chunk_generic(source: str, rel_path: str, language: str) -> list[Chunk]:
    """Sliding window for languages we don't have AST/regex rules for."""
    lines = source.splitlines()
    if not lines:
        return []

    chunks: list[Chunk] = []
    i = 0
    while i < len(lines):
        end = min(i + _WINDOW_SIZE, len(lines))
        window = "\n".join(lines[i:end])
        chunks.append(
            Chunk(
                text=window,
                file_path=rel_path,
                start_line=i + 1,
                end_line=end,
                language=language,
                chunk_type="generic",
            )
        )
        if end >= len(lines):
            break
        i += _WINDOW_SIZE - _WINDOW_OVERLAP

    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_short_chunks(chunks: list[Chunk], min_chars: int = 100) -> list[Chunk]:
    """Merge adjacent chunks below *min_chars* to avoid tiny embeddings."""
    if not chunks:
        return chunks
    merged: list[Chunk] = []
    acc = chunks[0]
    for c in chunks[1:]:
        if len(acc.text) < min_chars:
            acc = Chunk(
                text=acc.text + "\n" + c.text,
                file_path=acc.file_path,
                start_line=acc.start_line,
                end_line=c.end_line,
                language=acc.language,
                chunk_type=acc.chunk_type,
            )
        else:
            merged.append(acc)
            acc = c
    merged.append(acc)
    return merged
