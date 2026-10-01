"""Filesystem, process and task-local backend contracts for the bundled adapter."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from coderai.kaos import get_current_kaos, reset_current_kaos, set_current_kaos
from coderai.kaos.local import LocalKaos, local_kaos
from coderai.kaos.path import KaosPath


def test_canonical_path_preserves_symlink_components(tmp_path: Path) -> None:
    # Canonical is lexical normalization, not pathlib's symlink resolution.
    value = KaosPath(str(tmp_path)) / "link" / ".." / "café.txt"
    assert str(value.canonical()) == os.path.normpath(str(tmp_path / "café.txt"))
    assert value.canonical().relative_to(KaosPath(str(tmp_path))).name == "café.txt"


@pytest.mark.asyncio
async def test_backend_context_is_task_local(tmp_path: Path) -> None:
    class WorkspaceKaos(LocalKaos):
        def getcwd(self) -> KaosPath:
            return KaosPath(str(tmp_path))

        def gethome(self) -> KaosPath:
            return KaosPath(str(tmp_path / "home"))

    backend = WorkspaceKaos()
    entered, release = asyncio.Event(), asyncio.Event()

    async def child() -> None:
        token = set_current_kaos(backend)
        try:
            entered.set()
            await release.wait()
            assert get_current_kaos() is backend
            assert str(KaosPath("~/notes").expanduser()) == str(tmp_path / "home" / "notes")
            assert str(KaosPath("notes").canonical()) == str(tmp_path / "notes")
        finally:
            reset_current_kaos(token)

    task = asyncio.create_task(child())
    try:
        await entered.wait()
        assert get_current_kaos() is local_kaos
    finally:
        release.set()
        await task
    assert get_current_kaos() is local_kaos


@pytest.mark.asyncio
async def test_filesystem_round_trip(tmp_path: Path) -> None:
    directory = KaosPath(str(tmp_path / "nested"))
    await directory.mkdir(parents=True)
    text = directory / "café.txt"
    assert await text.write_text("café\r\n", encoding="latin-1") == 6
    assert await text.append_text("fin\n", encoding="latin-1") == 4
    assert await text.read_bytes(4) == b"caf\xe9"
    assert await text.read_text(encoding="latin-1") == "café\nfin\n"
    assert [line async for line in text.read_lines(encoding="latin-1")] == ["café\n", "fin\n"]
    binary = directory / "data.bin"
    assert await binary.write_bytes(b"\x00\xff") == 2
    assert await binary.read_bytes() == b"\x00\xff"
    assert await directory.is_dir() and await text.is_file()
    assert {p.name async for p in directory.iterdir()} == {"café.txt", "data.bin"}
    assert [p.name async for p in directory.glob("*.TXT", case_sensitive=False)] == ["café.txt"]
    assert (await text.stat()).st_size == 10
    missing = directory / "missing"
    assert not await missing.exists()
    assert not await missing.is_file()


@pytest.mark.asyncio
async def test_process_stdio_environment_and_reaping() -> None:
    process = await local_kaos.exec(
        sys.executable,
        "-c",
        "import os,sys; print(os.environ['KAOS_TEST']); print(sys.stdin.read()); "
        "print('error', file=sys.stderr)",
        env={**os.environ, "KAOS_TEST": "isolated"},
    )
    try:
        process.stdin.write(b"input")
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(process.stdout.read(), process.stderr.read()), timeout=10
        )
        assert stdout.decode().splitlines() == ["isolated", "input"]
        assert stderr.decode().splitlines() == ["error"]
        assert await process.wait() == process.returncode == 0
    finally:
        if process.returncode is None:
            await process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_process_kill_reaps_child() -> None:
    process = await local_kaos.exec(
        sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), timeout=10)).rstrip() == b"ready"
        await process.kill()
        assert await asyncio.wait_for(process.wait(), timeout=10) != 0
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            await process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_exec_requires_program() -> None:
    with pytest.raises(ValueError, match="At least one argument"):
        await local_kaos.exec()
