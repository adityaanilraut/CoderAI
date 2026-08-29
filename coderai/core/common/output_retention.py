"""Bounded output retention primitives matching DeepSeek Harness specification.

Provides zero-dependency ItemRetainer and TextRetainer for symmetric head/tail
slicing and standardized omission notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetainedItems(Generic[T]):
    """Result of bounded item retention."""

    items: list[T]
    total_count: int
    retained_count: int
    omitted_count: int
    notice: str | None = None


@dataclass(frozen=True)
class RetainedText:
    """Result of bounded text retention."""

    text: str
    total_chars: int
    total_lines: int
    retained_chars: int
    retained_lines: int
    omitted_chars: int
    omitted_lines: int
    notice: str | None = None


class ItemRetainer:
    """Symmetrically retains head and tail items when a sequence exceeds max_items."""

    def __init__(self, max_items: int = 100) -> None:
        self.max_items = max(1, max_items)

    def retain(self, items: Sequence[T]) -> RetainedItems[T]:
        total = len(items)
        if total <= self.max_items:
            return RetainedItems(
                items=list(items),
                total_count=total,
                retained_count=total,
                omitted_count=0,
                notice=None,
            )

        head_count = self.max_items // 2
        tail_count = self.max_items - head_count
        omitted = total - (head_count + tail_count)

        head = list(items[:head_count])
        tail = list(items[-tail_count:]) if tail_count > 0 else []
        notice = f"... [{omitted} items omitted] ..."

        return RetainedItems(
            items=head + tail,
            total_count=total,
            retained_count=len(head) + len(tail),
            omitted_count=omitted,
            notice=notice,
        )


class TextRetainer:
    """Symmetrically retains head and tail text lines/characters when content exceeds limits."""

    def __init__(
        self,
        max_chars: int = 30_000,
        max_lines: int = 1000,
        line_oriented: bool = True,
    ) -> None:
        self.max_chars = max(100, max_chars)
        self.max_lines = max(10, max_lines)
        self.line_oriented = line_oriented

    def retain(self, text: str) -> RetainedText:
        if not text:
            return RetainedText(
                text="",
                total_chars=0,
                total_lines=0,
                retained_chars=0,
                retained_lines=0,
                omitted_chars=0,
                omitted_lines=0,
                notice=None,
            )

        total_chars = len(text)
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        if total_chars <= self.max_chars and total_lines <= self.max_lines:
            return RetainedText(
                text=text,
                total_chars=total_chars,
                total_lines=total_lines,
                retained_chars=total_chars,
                retained_lines=total_lines,
                omitted_chars=0,
                omitted_lines=0,
                notice=None,
            )

        if self.line_oriented and total_lines > 1:
            head_lines_count = self.max_lines // 2
            tail_lines_count = self.max_lines - head_lines_count
            omitted_lines = max(0, total_lines - (head_lines_count + tail_lines_count))

            head_lines = lines[:head_lines_count]
            tail_lines = lines[-tail_lines_count:] if tail_lines_count > 0 else []

            head_text = "".join(head_lines)
            tail_text = "".join(tail_lines)

            # Check if combined char count still exceeds max_chars
            if len(head_text) + len(tail_text) > self.max_chars:
                head_chars = self.max_chars // 2
                tail_chars = self.max_chars - head_chars
                head_text = text[:head_chars]
                tail_text = text[-tail_chars:] if tail_chars > 0 else ""

            omitted_chars = max(0, total_chars - (len(head_text) + len(tail_text)))
            notice = f"\n\n... [{omitted_lines} lines / {omitted_chars} bytes omitted] ...\n\n"
            combined = f"{head_text.rstrip()}{notice}{tail_text.lstrip()}"

            return RetainedText(
                text=combined,
                total_chars=total_chars,
                total_lines=total_lines,
                retained_chars=len(head_text) + len(tail_text),
                retained_lines=len(head_lines) + len(tail_lines),
                omitted_chars=omitted_chars,
                omitted_lines=omitted_lines,
                notice=notice.strip(),
            )
        else:
            head_chars = self.max_chars // 2
            tail_chars = self.max_chars - head_chars
            omitted_chars = total_chars - (head_chars + tail_chars)

            head_text = text[:head_chars]
            tail_text = text[-tail_chars:] if tail_chars > 0 else ""
            notice = f"\n\n... [{omitted_chars} bytes omitted] ...\n\n"
            combined = f"{head_text}{notice}{tail_text}"

            return RetainedText(
                text=combined,
                total_chars=total_chars,
                total_lines=total_lines,
                retained_chars=len(head_text) + len(tail_text),
                retained_lines=len(combined.splitlines()),
                omitted_chars=omitted_chars,
                omitted_lines=0,
                notice=notice.strip(),
            )
