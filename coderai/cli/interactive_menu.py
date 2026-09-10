# Re-export shim: implementation moved to coderai.ui.shell.session_picker
# (kimi structure). Writes (mock.patch / monkeypatch) forward to the
# implementation module. Do not add code here.
from coderai._moved import forward as _forward

_forward(__name__, "coderai.ui.shell.session_picker")
