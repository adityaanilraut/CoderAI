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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "encoding", ["utf-8", "UTF_8", "utf16le", "UTF-16-LE", "latin-1", "cp1252"]
)
async def test_requested_codec_round_trip_and_byte_count(tmp_path, encoding, asynchronous):
    target = tmp_path / "codec.txt"
    text = "café"
    codec = "utf-16-le" if encoding == "utf16le" else encoding
    expected = text.encode(codec)
    if asynchronous:
        count = await async_atomic_write_text(target, text, encoding=encoding)
    else:
        count = atomic_write_text(target, text, encoding=encoding)
    assert count == len(expected)
    assert target.read_bytes() == expected
    assert target.read_bytes().decode(codec) == text
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_strict_encoding_failure_preserves_old_file(tmp_path, asynchronous):
    target = tmp_path / "old.txt"
    target.write_bytes(b"old")
    target.chmod(0o640)
    with pytest.raises(UnicodeEncodeError):
        if asynchronous:
            await async_atomic_write_text(target, "snowman ☃", encoding="ascii", errors="strict")
        else:
            atomic_write_text(target, "snowman ☃", encoding="ascii", errors="strict")
    assert target.read_bytes() == b"old"
    assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_encoding_replacement_policy(tmp_path, asynchronous):
    target = tmp_path / "replace.txt"
    if asynchronous:
        count = await async_atomic_write_text(target, "café", encoding="ascii", errors="replace")
    else:
        count = atomic_write_text(target, "café", encoding="ascii", errors="replace")
    assert target.read_bytes() == b"caf?"
    assert count == 4


def test_low_level_codec_and_replace_failure_cleanup(tmp_path, monkeypatch):
    import os
    from coderai.utils.path import write_file_atomic

    target = tmp_path / "file.txt"
    assert write_file_atomic(target, "café", encoding="latin-1") == 4
    assert target.read_bytes() == b"caf\xe9"
    target.chmod(0o640)

    def fail_replace(*args):
        raise OSError("replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        write_file_atomic(target, "new")
    assert target.read_bytes() == b"caf\xe9"
    assert target.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("encoding", ["not-a-codec", "ascii"])
def test_codec_failure_leaves_no_new_target_or_temporary(tmp_path, encoding):
    target = tmp_path / "new.txt"
    with pytest.raises((LookupError, UnicodeEncodeError)):
        atomic_write_text(target, "☃", encoding=encoding)
    assert not target.exists() and not list(tmp_path.iterdir())


def test_general_atomic_modes_remain_preserved_and_explicit(tmp_path):
    import os
    import stat
    from coderai.utils.path import write_file_atomic

    target = tmp_path / "general.txt"
    old_umask = os.umask(0)
    try:
        write_file_atomic(target, "new")
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        target.chmod(0o640)
        write_file_atomic(target, "overwrite")
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        write_file_atomic(target, "private", mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "iso2022_jp"])
def test_streamed_atomic_write_preserves_codec_and_commits_after_iteration(tmp_path, encoding):
    target = tmp_path / "streamed.txt"
    target.write_text("old")
    chunks = ["café" if encoding != "iso2022_jp" else "日本", " text", "\n"]

    def stream():
        for chunk in chunks:
            assert target.read_text() == "old"
            yield chunk

    count = atomic_write_text(target, stream(), encoding=encoding)
    expected = "".join(chunks).encode(encoding)
    assert target.read_bytes() == expected
    assert count == len(expected)
    assert list(tmp_path.iterdir()) == [target]


def test_streamed_atomic_write_failure_keeps_original_and_cleans_temporary(tmp_path):
    target = tmp_path / "streamed.txt"
    target.write_text("old")

    def fail():
        yield "partial"
        raise RuntimeError("source failed")

    with pytest.raises(RuntimeError, match="source failed"):
        atomic_write_text(target, fail())
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_can_replace_symlink_without_touching_destination(tmp_path):
    destination = tmp_path / "destination.txt"
    destination.write_text("untouched")
    link = tmp_path / "link.txt"
    link.symlink_to(destination)
    atomic_write_text(link, iter(["new", " text"]), mode=0o600, follow_symlinks=False)
    assert not link.is_symlink()
    assert link.read_text() == "new text"
    assert destination.read_text() == "untouched"
