"""Isolated fallback regex worker. The parent owns its deadline and output cap."""

from __future__ import annotations
import json
import sys
from dataclasses import asdict
from coderai.tools.file._search_common import SearchError
from coderai.tools.file.grep import _python_grep_worker


def main() -> None:
    try:
        matches = _python_grep_worker(**json.loads(sys.argv[1]))
        json.dump({"matches": [asdict(match) for match in matches]}, sys.stdout)
    except SearchError as exc:
        json.dump({"error": exc.message, "code": exc.code}, sys.stdout)


if __name__ == "__main__":
    main()
