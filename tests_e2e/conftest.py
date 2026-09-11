"""Pytest configuration and fixtures for tests_e2e."""

from __future__ import annotations

import sys
from typing import Any

# Ensure inline_snapshot is available for snapshot testing without external package install
if "inline_snapshot" not in sys.modules:
    try:
        import inline_snapshot  # noqa: F401
    except ImportError:
        import types

        class _Snapshot:
            def __init__(self, value: Any = None) -> None:
                self._value = value

            def __eq__(self, other: Any) -> bool:
                return other == self._value

            def __repr__(self) -> str:
                return repr(self._value)

        def _snapshot_fn(value: Any = None) -> _Snapshot:
            return _Snapshot(value)

        _mod = types.ModuleType("inline_snapshot")
        _mod.snapshot = _snapshot_fn  # type: ignore[attr-defined]
        sys.modules["inline_snapshot"] = _mod
