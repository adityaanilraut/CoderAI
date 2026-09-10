# Ported from coderai/core/wire/serde.py - kimi structure (kimi_cli/wire/serde.py).
"""Wire message serde (Kimi ``wire/serde.py`` parity).

Thin re-export over :mod:`coderai.wire.types` so wire consumers import
from one canonical location, matching the Kimi layout.
"""

from __future__ import annotations

from coderai.wire.types import deserialize_wire_message, serialize_wire_message

__all__ = ["serialize_wire_message", "deserialize_wire_message"]
