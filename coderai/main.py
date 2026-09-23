"""Console entry point — delegates to the CLI layer with crash handlers."""

from __future__ import annotations

from coderai.cli.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
