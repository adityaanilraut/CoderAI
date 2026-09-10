"""Moved to coderai.wire - shim (LAZY: old-becomes-shim; content merged into coderai/wire/__init__.py)."""
from coderai.wire import *  # moved


def __getattr__(name: str):
    import importlib

    return getattr(importlib.import_module("coderai.wire"), name)
