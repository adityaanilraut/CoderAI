"""CoderAI package."""

try:
    from coderai._version import __version__  # type: ignore
except ImportError:
    from coderAI._version import __version__  # type: ignore

__all__ = ["__version__"]
