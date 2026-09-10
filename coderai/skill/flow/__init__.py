"""Kimi-structure mirror of the flow skill package (``kimi_cli/skill/flow``).

The implementation lives in :mod:`coderai.core.flow` (ported verbatim from
Kimi in a prior session, with live consumers in ``core/session.py`` and
``cli/app.py``); this package forwards there so the Kimi path works too.
Reads fall through to the implementation, and writes (``mock.patch``,
``monkeypatch.setattr`` against this path) land on the implementation.
"""
from coderai._moved import forward as _forward

_forward(__name__, "coderai.core.flow")
