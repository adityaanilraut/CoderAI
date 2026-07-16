"""CoderAI package."""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("coderai-agent")
    except PackageNotFoundError:  # pragma: no cover - editable/source tree
        __version__ = "0.3.2"
except ImportError:  # pragma: no cover - Python < 3.8
    __version__ = "0.3.2"

__all__ = ["__version__"]
