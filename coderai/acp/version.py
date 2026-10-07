from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ACPVersionSpec:
    """Describes one supported ACP protocol version."""

    protocol_version: int  # negotiation integer (currently 1)
    spec_tag: str  # ACP spec tag (e.g. "v0.10.8")
    sdk_version: str  # corresponding SDK version (e.g. "0.8.0")


CURRENT_VERSION = ACPVersionSpec(
    protocol_version=1,
    spec_tag="v0.10.8",
    sdk_version="0.8.0",
)


def negotiate_version(client_protocol_version: int) -> ACPVersionSpec:
    """Advertise the sole supported version; the client may disconnect if incompatible."""
    return CURRENT_VERSION
