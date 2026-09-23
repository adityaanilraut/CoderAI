"""Unit tests verifying atomic file-write guarantees."""

from __future__ import annotations

import pathlib
import pytest

from coderai.utils.io import atomic_write_text, async_atomic_write_text


def test_atomic_write_text_creates_and_overwrites(tmp_path: pathlib.Path):
    target = tmp_path / "subdir" / "test.txt"
    written = atomic_write_text(target, "Hello Atomic World!")
    assert written == len("Hello Atomic World!")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "Hello Atomic World!"

    # Overwrite
    written2 = atomic_write_text(target, "Updated Line")
    assert written2 == len("Updated Line")
    assert target.read_text(encoding="utf-8") == "Updated Line"

    # Verify no temporary files left in the directory
    temp_files = [f for f in target.parent.iterdir() if f.name.endswith(".tmp")]
    assert len(temp_files) == 0


@pytest.mark.asyncio
async def test_async_atomic_write_text(tmp_path: pathlib.Path):
    target = tmp_path / "async_test.txt"
    written = await async_atomic_write_text(target, "Async Content")
    assert written == len("Async Content")
    assert target.read_text(encoding="utf-8") == "Async Content"
