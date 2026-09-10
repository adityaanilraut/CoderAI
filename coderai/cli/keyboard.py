"""Moved to coderai.ui.shell.keyboard - shim (kimi structure). Reads and writes
(mock.patch / monkeypatch) forward to the implementation module.
Do not add code here."""
from coderai._moved import forward as _forward

_forward(__name__, "coderai.ui.shell.keyboard")
