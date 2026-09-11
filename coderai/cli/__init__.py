"""coderai.cli — thin UI layer over the CoderAI modular engine."""

from typing import Any, Literal

UIMode = Literal["shell", "print", "acp", "wire"]


class ExitCode:
    SUCCESS = 0
    FAILURE = 1
    RETRYABLE = 75  # EX_TEMPFAIL from sysexits.h


InputFormat = Literal["text", "stream-json"]
OutputFormat = Literal["text", "stream-json"]

__all__ = ["main", "UIMode", "ExitCode", "InputFormat", "OutputFormat"]


def __getattr__(name: str) -> Any:
    # Lazy so importing any coderai.cli.* submodule (e.g. coderai.cli.elapsed
    # via coderai.ui.shell.visualize._blocks) doesn't eagerly import the
    # interactive app monolith and create import cycles through moved shims.
    if name == "main":
        from coderai.ui.shell.app import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
