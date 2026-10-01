# Adapted from PyKAOS 0.9.0, Copyright 2025 Moonshot AI (Apache-2.0).
# CoderAI changes: namespaced imports, local/ACP-only scope, Python typing compatibility.
from contextvars import ContextVar

from coderai.kaos import Kaos
from coderai.kaos.local import local_kaos

current_kaos = ContextVar[Kaos]("current_kaos", default=local_kaos)
