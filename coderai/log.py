"""Central logging (Kimi ``utils/logging.py`` + ``__init__.py`` parity).

Uses ``loguru`` when installed (lazy import, disabled by default for library
use) and falls back to stdlib ``logging`` otherwise — so the test interpreter
and minimal installs work with zero extra dependencies.

Application entry points call :func:`enable_logging` once; library code only
uses the ``logger`` singleton.
"""

from __future__ import annotations

import codecs
import contextlib
import locale
import logging
import os
import sys
import threading
from collections.abc import Iterator
from typing import IO, Any

_STD_LOGGER = logging.getLogger("coderai")


class _StdLoggerAdapter:
    """Minimal loguru-compatible surface over stdlib logging."""

    def __init__(self, inner: logging.Logger = _STD_LOGGER) -> None:
        self._inner = inner

    # loguru compat shims (no-ops where stdlib has no equivalent)
    def opt(self, *args: Any, **kwargs: Any) -> _StdLoggerAdapter:
        return self

    def bind(self, *args: Any, **kwargs: Any) -> _StdLoggerAdapter:
        return self

    def enable(self, *args: Any, **kwargs: Any) -> None:
        return None

    def disable(self, *args: Any, **kwargs: Any) -> None:
        return None

    def remove(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def configure(self, *args: Any, **kwargs: Any) -> None:
        return None

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._inner.debug(str(msg), *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._inner.info(str(msg), *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._inner.warning(str(msg), *args, **kwargs)

    warn = warning

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._inner.error(str(msg), *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._inner.exception(str(msg), *args, **kwargs)

    def log(self, level: Any, msg: Any, *args: Any, **kwargs: Any) -> None:
        try:
            self._inner.log(int(level), str(msg), *args, **kwargs)
        except (TypeError, ValueError):
            self._inner.info(str(msg), *args, **kwargs)


class _LazyLogger:
    """Import loguru only when logging is actually used."""

    def __init__(self) -> None:
        self._logger: Any | None = None

    def _get(self) -> Any:
        if self._logger is None:
            try:
                from loguru import logger as real_logger

                real_logger.disable("coderai")
                self._logger = real_logger
            except ImportError:
                self._logger = _StdLoggerAdapter()
        return self._logger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


logger: Any = _LazyLogger()

__all__ = [
    "logger",
    "enable_logging",
    "redirect_stderr_to_logger",
    "restore_stderr",
    "open_original_stderr",
]


def enable_logging(debug: bool = False, *, redirect_stderr: bool = True) -> None:
    """Enable file logging under the share dir; optionally capture fd=2."""
    from coderai.core.share import get_share_dir

    inner = _LazyLogger()._get()  # noqa: SLF001 - same-module access
    if hasattr(inner, "remove"):
        try:
            inner.remove()
        except Exception:
            pass
    log_file = get_share_dir() / "logs" / "coderai.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if type(inner).__name__ == "Logger":  # real loguru
        try:
            inner.enable("coderai")
            inner.add(
                str(log_file),
                level="TRACE" if debug else "INFO",
                format=(
                    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
                    "{name}:{function}:{line} - {message}"
                ),
                rotation="06:00",
                retention="10 days",
            )
        except Exception:
            pass
    else:
        _STD_LOGGER.setLevel(logging.DEBUG if debug else logging.INFO)
        if not _STD_LOGGER.handlers:
            try:
                handler = logging.FileHandler(str(log_file), encoding="utf-8")
                handler.setFormatter(
                    logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
                )
                _STD_LOGGER.addHandler(handler)
            except OSError:
                pass
    if redirect_stderr:
        redirect_stderr_to_logger()


class StderrRedirector:
    """Capture process fd=2 into the logger via a pipe + drain thread."""

    def __init__(self, level: str = "ERROR") -> None:
        self._level = level
        self._encoding: str | None = None
        self._installed = False
        self._lock = threading.Lock()
        self._original_fd: int | None = None
        self._read_fd: int | None = None
        self._thread: threading.Thread | None = None

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            with contextlib.suppress(Exception):
                sys.stderr.flush()
            if self._original_fd is None:
                with contextlib.suppress(OSError):
                    self._original_fd = os.dup(2)
            if self._encoding is None:
                self._encoding = (
                    sys.stderr.encoding or locale.getpreferredencoding(False) or "utf-8"
                )
            read_fd, write_fd = os.pipe()
            os.dup2(write_fd, 2)
            os.close(write_fd)
            self._read_fd = read_fd
            self._thread = threading.Thread(
                target=self._drain, name="coderai-stderr-redirect", daemon=True
            )
            self._thread.start()
            self._installed = True

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            if self._original_fd is not None:
                with contextlib.suppress(OSError):
                    os.dup2(self._original_fd, 2)
            self._installed = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _drain(self) -> None:
        buffer = ""
        read_fd = self._read_fd
        if read_fd is None:
            return
        encoding = self._encoding or "utf-8"
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        try:
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                buffer += decoder.decode(chunk)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    self._log_line(line)
        except Exception:
            logger.exception("Failed to read redirected stderr")
        finally:
            buffer += decoder.decode(b"", final=True)
            if buffer:
                self._log_line(buffer)
            with contextlib.suppress(OSError):
                os.close(read_fd)

    def _log_line(self, line: str) -> None:
        text = line.rstrip("\r")
        if not text:
            return
        logger.opt(depth=2).log(self._level, text)

    def open_original_stderr_handle(self) -> IO[bytes] | None:
        if self._original_fd is None:
            return None
        dup_fd = os.dup(self._original_fd)
        os.set_inheritable(dup_fd, True)
        return os.fdopen(dup_fd, "wb", closefd=True)


_stderr_redirector: StderrRedirector | None = None


def redirect_stderr_to_logger(level: str = "ERROR") -> None:
    """Install the fd=2 → logger redirector (idempotent)."""
    global _stderr_redirector
    if _stderr_redirector is None:
        _stderr_redirector = StderrRedirector(level=level)
    _stderr_redirector.install()


def restore_stderr() -> None:
    """Restore fd=2 captured by :func:`redirect_stderr_to_logger`."""
    if _stderr_redirector is not None:
        _stderr_redirector.uninstall()


@contextlib.contextmanager
def open_original_stderr() -> Iterator[IO[bytes] | None]:
    """Yield the pre-redirect fd=2 handle (None when not installed)."""
    redirector = _stderr_redirector
    if redirector is None:
        yield None
        return
    stream = redirector.open_original_stderr_handle()
    try:
        yield stream
    finally:
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
