"""Backward-compatible re-export — prefer ``coderAI.types.provenance``."""

from coderAI.types.provenance import *  # noqa: F403
from coderAI.types.provenance import (
    Provenance,
    UNTRUSTED_CLOSE_TAG,
    UNTRUSTED_OPEN_TAG,
    fence_project_context,
    wrap_untrusted_output,
)

__all__ = [
    "Provenance",
    "wrap_untrusted_output",
    "fence_project_context",
    "UNTRUSTED_OPEN_TAG",
    "UNTRUSTED_CLOSE_TAG",
]
