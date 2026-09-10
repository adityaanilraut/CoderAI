# Re-export shim: implementation moved to coderai.subagents.* (kimi structure).
# Reads and writes (mock.patch / monkeypatch / direct assignment) go through
# coderai.subagents.runner, where the engine looks its globals up.
# Do not add code here.
from coderai._moved import forward as _forward

_forward(__name__, "coderai.subagents.runner")
