"""Dynamic path locking and file registry for concurrent multi-agent filesystem access."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


class PathLockEntry:
    """Read-Write lock state for a single canonical file system path."""

    def __init__(self, canonical_path: str) -> None:
        self.path = canonical_path
        self._write_lock = asyncio.Lock()
        self._read_count = 0
        self._read_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def acquire_read(self) -> AsyncIterator[None]:
        """Acquire shared read access. Multiple readers can execute concurrently."""
        async with self._read_lock:
            self._read_count += 1
            if self._read_count == 1:
                # First reader acquires the write lock to block incoming writers
                await self._write_lock.acquire()
        try:
            yield
        finally:
            async with self._read_lock:
                self._read_count -= 1
                if self._read_count == 0:
                    # Last reader releases the write lock
                    self._write_lock.release()

    @contextlib.asynccontextmanager
    async def acquire_write(self) -> AsyncIterator[None]:
        """Acquire exclusive write access. Blocks all other readers and writers."""
        await self._write_lock.acquire()
        try:
            yield
        finally:
            self._write_lock.release()


class PathLockManager:
    """Manages fine-grained, per-path async locks across parallel tool workers and subagents."""

    def __init__(self) -> None:
        self._locks: dict[str, PathLockEntry] = {}
        self._registry_lock = asyncio.Lock()

    def _canonicalize(self, raw_path: str | pathlib.Path, project_root: str | None = None) -> str:
        p = pathlib.Path(raw_path)
        if not p.is_absolute() and project_root:
            p = pathlib.Path(project_root) / p
        try:
            return str(p.resolve())
        except Exception:
            return str(p.absolute())

    async def _get_or_create_entry(self, canonical_path: str) -> PathLockEntry:
        async with self._registry_lock:
            if canonical_path not in self._locks:
                self._locks[canonical_path] = PathLockEntry(canonical_path)
            return self._locks[canonical_path]

    @contextlib.asynccontextmanager
    async def acquire_read_lock(
        self, file_path: str | pathlib.Path, project_root: str | None = None
    ) -> AsyncIterator[None]:
        """Acquire non-exclusive read lock for a specific path."""
        cpath = self._canonicalize(file_path, project_root)
        entry = await self._get_or_create_entry(cpath)
        async with entry.acquire_read():
            yield

    @contextlib.asynccontextmanager
    async def acquire_write_lock(
        self, file_path: str | pathlib.Path, project_root: str | None = None
    ) -> AsyncIterator[None]:
        """Acquire exclusive write lock for a specific path."""
        cpath = self._canonicalize(file_path, project_root)
        entry = await self._get_or_create_entry(cpath)
        async with entry.acquire_write():
            yield


_global_path_lock_manager = PathLockManager()


def get_path_lock_manager() -> PathLockManager:
    """Return the singleton PathLockManager instance."""
    return _global_path_lock_manager
