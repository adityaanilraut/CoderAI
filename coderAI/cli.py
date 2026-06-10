"""CLI entry point for CoderAI.

Delegates to ``coderAI.cli.main`` which contains the full CLI
implementation split across multiple modules.
"""

from coderAI.cli.main import cli, main

__all__ = ["cli", "main"]

if __name__ == "__main__":
    main()
