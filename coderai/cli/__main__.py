"""Entry for ``python -m coderai.cli``.

Enables subcommand-style invocation from a source checkout or from an ACP
client configuration that shells out to ``python -m coderai.cli acp``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, installing crash handlers for the whole process."""
    from coderai.ui.shell.app import main as run_cli
    from coderai.telemetry.crash import install_crash_handlers, set_phase

    # Installed first so startup-phase crashes are captured (parity).
    install_crash_handlers()
    try:
        return int(run_cli(list(argv) if argv is not None else sys.argv[1:]) or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        set_phase("shutdown")


if __name__ == "__main__":
    raise SystemExit(main())
