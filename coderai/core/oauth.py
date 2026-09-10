# Re-export shim: implementation moved to coderai.auth.oauth (+ platforms)
# (kimi structure). Writes (mock.patch / monkeypatch) forward to the
# implementation module. Do not add code here.
from coderai.auth.platforms import *  # noqa: F401,F403
from coderai._moved import forward as _forward

_forward(__name__, "coderai.auth.oauth")
