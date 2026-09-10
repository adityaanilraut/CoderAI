# Re-export shim: implementation moved to coderai.config (settings +
# typed_config merged, kimi structure). Writes (mock.patch / monkeypatch)
# forward to the implementation module. Do not add code here.
from coderai._moved import forward as _forward

_forward(__name__, "coderai.config")
