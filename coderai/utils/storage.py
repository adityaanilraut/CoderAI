"""Owned storage paths and descriptor-relative I/O without symlink traversal."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import stat
import sys

if sys.platform != "win32":
    import fcntl
from pathlib import Path
from collections.abc import Iterator


def storage_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError("Invalid storage identifier")
    if ".." in value:
        raise ValueError("Invalid storage identifier")
    return value


def owned_path(root: Path | str, *parts: str) -> Path:
    """Reject links below a caller-owned, canonical root before any side effect."""
    path = Path(root).absolute()
    for part in parts:
        if part in {".", "..", ""} or "/" in part or "\\" in part:
            raise ValueError("Invalid storage path component")
        path /= part
        if path.is_symlink():
            raise ValueError("Storage paths must not contain symlinks")
    if not path.resolve().is_relative_to(Path(root).absolute()):
        raise ValueError("Storage path escapes its owner")
    return path


@contextlib.contextmanager
def parent_directory(path: Path, *, create: bool = False) -> Iterator[int | None]:
    """Pin every directory during I/O so swapping an ancestor cannot redirect it.

    POSIX uses openat/O_NOFOLLOW. On platforms without directory descriptors,
    reject links and revalidate paths; native Windows reparse-point protection
    remains the responsibility of its filesystem permissions.
    """
    path = path.absolute()
    if os.open not in os.supports_dir_fd:
        parent = path.parent
        if create:
            parent.mkdir(parents=True, exist_ok=True)
        if parent.resolve() != parent or path.is_symlink():
            raise ValueError("Storage paths must not contain symlinks")
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_bytes(path: Path, *, limit: int | None = None) -> bytes:
    with parent_directory(path) as directory:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path if directory is None else path.name, flags, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Storage input must be a regular file")
            data = stream.read() if limit is None else stream.read(limit + 1)
            if limit is not None and len(data) > limit:
                raise ValueError("Storage input exceeds its size limit")
            return data


def write_bytes(path: Path, data: bytes, *, append: bool = False, mode: int = 0o600) -> None:
    with parent_directory(path, create=True) as directory:
        target = path if directory is None else path.name
        if append:
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            fd = os.open(target, flags, mode, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Storage output must be a regular file")
                stream.write(data)
            return
        temporary = f".{path.name}.{secrets.token_hex(8)}.tmp"
        temporary_path = path.parent / temporary if directory is None else temporary
        fd = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path, dir_fd=directory)


def remove_path(path: Path, *, tree: bool = False) -> None:
    try:
        with parent_directory(path) as directory:
            if tree:
                if directory is None:
                    if path.is_symlink():
                        raise ValueError("Refusing to remove a symlinked directory")
                    shutil.rmtree(path)
                else:
                    shutil.rmtree(path.name, dir_fd=directory)
            else:
                os.unlink(path if directory is None else path.name, dir_fd=directory)
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def storage_lock(path: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """Serialize owned-store transactions without following a lock-file link."""
    import time

    with parent_directory(path, create=True) as directory:
        lock_path = (
            path.parent / (path.name + ".lock") if directory is None else path.name + ".lock"
        )
        try:
            fd = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Storage lock must be a regular file")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for storage transaction")
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)
