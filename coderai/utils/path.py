""""""

from __future__ import annotations

import os
import pathlib
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")

LOCK_RETRY_INITIAL_S = 0.02
LOCK_RETRY_MAX_S = 0.2
LOCK_TIMEOUT_S = 2.0


@dataclass
class WriteFileAtomicOptions:
    mode: int = 0o644
    dir_mode: int | None = None
    encoding: str = "utf8"


def normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n")


def normalize_line_endings(value: str) -> str:
    """Normalize CRLF and CR to LF."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def detect_line_endings(value: str) -> str:
    return "CRLF" if "\r\n" in value else "LF"


def detect_encoding(buf: bytes) -> str:
    if len(buf) >= 2 and buf[0] == 0xFF and buf[1] == 0xFE:
        return "utf16le"
    return "utf8"


def is_binary_buffer(buf: bytes, sample_size: int = 8192, threshold: float = 0.3) -> bool:
    """Detect whether a raw byte buffer contains binary data.

    Returns True if null bytes are found or non-printable character ratio exceeds threshold.
    """
    if not buf:
        return False
    sample = buf[:sample_size]
    if b"\x00" in sample:
        return True
    non_text = sum(1 for b in sample if b < 32 and b not in (9, 10, 13))
    return (non_text / len(sample)) > threshold


def read_text_file_with_metadata(path: str) -> dict[str, Any]:
    p = pathlib.Path(path)
    raw = p.read_bytes()
    enc = detect_encoding(raw)
    text = raw.decode("utf-16-le" if enc == "utf16le" else "utf-8", errors="replace")
    content = normalize_content(text)
    return {
        "content": content,
        "encoding": enc,
        "lineEndings": detect_line_endings(text),
        "timestamp": int(p.stat().st_mtime * 1000),
    }


def write_file_atomic(
    filename: str | pathlib.Path,
    content: str,
    mode: int | None = None,
    dir_mode: int | None = None,
    encoding: str = "utf8",
) -> int:
    """Replace filename with content in one atomic step, creating parent directories.

    with exclusive create (O_CREAT | O_EXCL | O_WRONLY), preserves or sets mode bits,
    flushes and syncs buffer, and atomically renames over target.
    """
    target = pathlib.Path(filename).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if dir_mode is not None:
        try:
            os.chmod(target.parent, dir_mode)
        except OSError:
            pass

    target_mode = mode
    if target_mode is None:
        if target.exists():
            try:
                target_mode = stat.S_IMODE(target.stat().st_mode)
            except OSError:
                target_mode = 0o644
        else:
            target_mode = 0o644

    target_encoding = "utf-16-le" if encoding == "utf16le" else "utf-8"
    encoded_bytes = content.encode(target_encoding)

    temp_name = f"{target.name}.{secrets.token_hex(6)}.tmp"
    temp_path = target.parent / temp_name

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    fd = os.open(str(temp_path), flags, target_mode)
    try:
        with open(fd, "wb", closefd=True) as f:
            f.write(encoded_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

        try:
            os.chmod(str(temp_path), target_mode)
        except OSError:
            pass

        os.replace(str(temp_path), str(target))
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    return len(encoded_bytes)


def with_file_lock(
    filename: str | pathlib.Path,
    operation: Callable[[], T],
    timeout_s: float = LOCK_TIMEOUT_S,
) -> T:
    """Hold cross-process writer lock for filename around an operation.

    with exclusive create, exponential backoff retry, and timeout protection.
    """
    target = pathlib.Path(filename).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f"{target.name}.lock"
    deadline = time.time() + timeout_s
    delay = LOCK_RETRY_INITIAL_S

    while True:
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(str(lock_path), flags, 0o600)
            try:
                with open(fd, "w", encoding="utf-8", closefd=True) as f:
                    f.write(f"{os.getpid()}\n")
            except Exception:
                pass
            break
        except FileExistsError:
            pass
        except OSError:
            if not lock_path.exists():
                raise

        if time.time() >= deadline:
            raise TimeoutError(f"atomic-write: timed out waiting for writer lock at {lock_path}")
        time.sleep(delay)
        delay = min(delay * 2, LOCK_RETRY_MAX_S)

    try:
        return operation()
    finally:
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass


def write_text_file(
    path: str, content: str, encoding: str = "utf8", line_endings: str = "LF"
) -> int:
    norm = normalize_content(content)
    to_write = norm.replace("\n", "\r\n") if line_endings == "CRLF" else norm
    return write_file_atomic(path, to_write, encoding=encoding)


def ensure_parent_directory(path: str) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)


def has_file_changed_since_state(path: str, state: Any) -> bool:
    try:
        cur = read_text_file_with_metadata(path)
        return cur["timestamp"] > state.timestamp
    except Exception:
        return True


def build_diff_preview(file_path: str, old_content: str | None, new_content: str) -> str:
    import difflib

    old_lines = (old_content or "").splitlines()
    new_lines = new_content.splitlines()
    diff = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{file_path}", tofile=f"b/{file_path}", lineterm=""
        )
    )
    return "\n".join(diff[:200])


def read_text_file_tail(path: str, max_chars: int = 4000) -> dict[str, Any] | None:
    """Read the trailing character slice of a file safely."""
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        if size == 0:
            return {"content": "", "total_bytes": 0, "truncated": False}
        with open(p, "rb") as f:
            chunk_size = min(size, max_chars * 4)
            if size > chunk_size:
                f.seek(size - chunk_size)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > max_chars or size > chunk_size
        if len(text) > max_chars:
            text = text[-max_chars:]
        return {
            "content": text.strip(),
            "total_bytes": size,
            "truncated": truncated,
        }
    except Exception:
        return None


import asyncio
import re
import aiofiles.os
from pathlib import Path, PurePath
from kaos.path import KaosPath

_ROTATION_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY
_ROTATION_FILE_MODE = 0o600


async def _reserve_rotation_path(path: Path) -> bool:
    """Atomically create an empty file as a reservation for *path*."""
    def _create() -> None:
        fd = os.open(str(path), _ROTATION_OPEN_FLAGS, _ROTATION_FILE_MODE)
        os.close(fd)

    try:
        await asyncio.to_thread(_create)
    except FileExistsError:
        return False
    return True


async def next_available_rotation(path: Path) -> Path | None:
    """Return a reserved rotation path for *path* or ``None`` if parent is missing."""
    if not path.parent.exists():
        return None

    base_name = path.stem
    suffix = path.suffix
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+){re.escape(suffix)}$")
    max_num = 0
    try:
        for entry in await aiofiles.os.listdir(path.parent):
            if match := pattern.match(entry):
                max_num = max(max_num, int(match.group(1)))
    except Exception:
        pass

    next_num = max_num + 1
    while True:
        next_path = path.parent / f"{base_name}_{next_num}{suffix}"
        if await _reserve_rotation_path(next_path):
            return next_path
        next_num += 1


def shorten_home(path: Any) -> Any:
    """Convert absolute path to use `~` for home directory."""
    try:
        if isinstance(path, KaosPath):
            home = KaosPath.home()
            p = path.relative_to(home)
            return KaosPath("~") / p
        p_obj = Path(str(path))
        home_obj = Path.home()
        rel = p_obj.relative_to(home_obj)
        return Path("~") / rel
    except Exception:
        return path


def normalize_user_path(raw: str) -> str:
    """Normalize a user-provided path string to a native form."""
    import platform

    if platform.system() != "Windows":
        return raw

    if raw.startswith("//") or raw.startswith("/cygdrive/"):
        return raw.lstrip("/")
    if len(raw) >= 2 and raw[0] == "/" and raw[1].isalpha() and (len(raw) == 2 or raw[2] == "/"):
        drive = raw[1].upper()
        rest = raw[2:].replace("/", "\\")
        return f"{drive}:{rest}"
    return raw


def kaos_path_from_user_input(raw: str) -> KaosPath:
    """Convert a model-supplied path string into a usable KaosPath."""
    return KaosPath(normalize_user_path(raw)).expanduser()


def sanitize_cli_path(raw: str) -> str:
    """Strip surrounding quotes from a CLI path argument."""
    raw = raw.strip()
    if len(raw) >= 2 and ((raw[0] == "'" and raw[-1] == "'") or (raw[0] == '"' and raw[-1] == '"')):
        raw = raw[1:-1]
    return raw


def is_within_directory(path: Any, directory: Any) -> bool:
    """Check whether path is contained within directory using pure path semantics."""
    candidate = PurePath(str(path))
    base = PurePath(str(directory))
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def is_within_workspace(
    path: Any,
    work_dir: Any,
    additional_dirs: Any = (),
) -> bool:
    """Check whether path is within the workspace (work_dir or any additional directory)."""
    if is_within_directory(path, work_dir):
        return True
    return any(is_within_directory(path, d) for d in (additional_dirs or []))


_LIST_DIR_ROOT_WIDTH = 30
_LIST_DIR_CHILD_WIDTH = 10


async def _collect_entries(
    dir_path: KaosPath, max_width: int
) -> tuple[list[tuple[str, bool]], int]:
    from stat import S_ISDIR

    all_entries: list[tuple[str, bool]] = []
    async for entry in dir_path.iterdir():
        try:
            st = await entry.stat()
            is_dir = S_ISDIR(st.st_mode)
        except OSError:
            is_dir = False
        all_entries.append((entry.name, is_dir))
    all_entries.sort(key=lambda e: (not e[1], e[0]))
    return all_entries[:max_width], len(all_entries)


async def list_directory(work_dir: KaosPath) -> str:
    """Return a compact tree listing of work_dir (up to 2 levels)."""
    lines: list[str] = []
    entries, total = await _collect_entries(work_dir, _LIST_DIR_ROOT_WIDTH)
    remaining = total - len(entries)

    for i, (name, is_dir) in enumerate(entries):
        is_last = (i == len(entries) - 1) and remaining == 0
        connector = "└── " if is_last else "├── "

        if is_dir:
            lines.append(f"{connector}{name}/")
            child_prefix = "    " if is_last else "│   "
            try:
                child_entries, child_total = await _collect_entries(
                    work_dir / name, _LIST_DIR_CHILD_WIDTH
                )
            except OSError:
                lines.append(f"{child_prefix}└── [not readable]")
                continue
            child_remaining = child_total - len(child_entries)
            for j, (child_name, child_is_dir) in enumerate(child_entries):
                child_is_last = (j == len(child_entries) - 1) and child_remaining == 0
                child_connector = "└── " if child_is_last else "├── "
                suffix = "/" if child_is_dir else ""
                lines.append(f"{child_prefix}{child_connector}{child_name}{suffix}")
            if child_remaining > 0:
                lines.append(f"{child_prefix}└── ... and {child_remaining} more")
        else:
            lines.append(f"{connector}{name}")

    if remaining > 0:
        lines.append(f"└── ... and {remaining} more")

    return "\n".join(lines)


async def find_project_root(work_dir: KaosPath) -> KaosPath:
    """Walk up from work_dir to find the nearest directory containing .git."""
    current = work_dir
    while True:
        if await (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return work_dir
        current = parent

