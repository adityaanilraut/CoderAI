"""Helpers for moved-module shims (kimi-structure mirror).

`forward()` turns the calling module into a transparent proxy of the new
implementation module. Reads fall through to the implementation, and —
critically — *writes* (`mock.patch`, `monkeypatch.setattr` against the old
import path) land on the implementation module, which is where the code under
test looks its globals up. A plain `from new import *` shim would swallow
such patches and leave tests hanging or silently unpatched.
"""

from __future__ import annotations

import sys
from types import ModuleType

_targets: dict[str, str] = {}


class _ForwardingModule(ModuleType):
    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(sys.modules[_targets[self.__name__]], name)

    def __setattr__(self, name: str, value) -> None:  # type: ignore[no-untyped-def]
        setattr(sys.modules[_targets[self.__name__]], name, value)

    def __delattr__(self, name: str) -> None:
        delattr(sys.modules[_targets[self.__name__]], name)


def forward(old_name: str, new_name: str) -> None:
    """Make module `old_name` a forwarding shim for `new_name`.

    No names are copied: every read goes through `__getattr__` to the live
    implementation module, so a patched attribute is visible no matter
    whether it is accessed before or after the patch. `__all__` is set so
    `from old import *` keeps working (it resolves each name via getattr).
    """
    __import__(new_name)
    impl = sys.modules[new_name]
    old = sys.modules[old_name]
    try:
        all_names = list(impl.__all__)  # type: ignore[attr-defined]
    except AttributeError:
        all_names = [k for k in vars(impl) if not k.startswith("_")]
    old.__dict__["__all__"] = all_names
    _targets[old_name] = new_name
    old.__class__ = _ForwardingModule
