"""Generate the tool catalog, or check that the checked-in catalog matches."""

from __future__ import annotations
import argparse
from pathlib import Path
from coderai.tools.legacy.catalog import render_catalog
from coderai.tools.legacy.registry import get_tool_registry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "docs" / "tools.md"
    expected = render_catalog(get_tool_registry())
    if args.check:
        return int(not target.is_file() or target.read_text() != expected)
    target.write_text(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
