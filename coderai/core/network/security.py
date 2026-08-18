"""Network Security & Policy Enforcement (SSRF protection, domain allowlist/blocklist)."""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


class NetworkSecurityError(Exception):
    """Raised when an outbound network request violates security policies."""


@dataclass
class NetworkPolicy:
    """Outbound network request policy and security configuration."""

    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    allow_private_ips: bool = False
    enforce_ssrf_protection: bool = True

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> NetworkPolicy:
        if not settings:
            return cls()
        net_cfg = settings.get("network") or {}
        return cls(
            allowed_domains=list(
                net_cfg.get("allowedDomains") or net_cfg.get("allowed_domains") or []
            ),
            blocked_domains=list(
                net_cfg.get("blockedDomains") or net_cfg.get("blocked_domains") or []
            ),
            allow_private_ips=bool(net_cfg.get("allowPrivateIps", False)),
            enforce_ssrf_protection=bool(net_cfg.get("enforceSsrfProtection", True)),
        )


def is_private_or_loopback_ip(ip_str: str) -> bool:
    """Check if an IP address is private, loopback, link-local, or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False


def is_domain_matching(domain: str, pattern: str) -> bool:
    """Match domain against a pattern, supporting wildcards like *.example.com."""
    domain = domain.lower().strip()
    pattern = pattern.lower().strip()

    if pattern == "*":
        return True
    if domain == pattern:
        return True

    # Handle wildcard patterns
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return domain == suffix or domain.endswith("." + suffix)

    return fnmatch.fnmatch(domain, pattern)


def validate_outbound_url(url: str, policy: NetworkPolicy | None = None) -> tuple[bool, str | None]:
    """Validate an outbound URL against network security policies (SSRF, allowlist/blocklist).

    Returns:
        (is_valid, error_reason)
    """
    policy = policy or NetworkPolicy()

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported URL scheme: '{parsed.scheme}'. Only http and https are allowed."

    hostname = parsed.hostname
    if not hostname:
        return False, "Invalid URL: missing hostname."

    hostname_lower = hostname.lower()

    # 1. Check blocked domains
    for blocked in policy.blocked_domains:
        if is_domain_matching(hostname_lower, blocked):
            return False, f"Access to domain '{hostname}' is blocked by security policy."

    # 2. Check SSRF for IP addresses or resolved hosts
    if policy.enforce_ssrf_protection and not policy.allow_private_ips:
        # Check if hostname is direct IP literal
        if is_private_or_loopback_ip(hostname_lower):
            return (
                False,
                f"Access to private/loopback IP '{hostname}' is forbidden (SSRF protection).",
            )

        if hostname_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False, f"Access to loopback address '{hostname}' is forbidden (SSRF protection)."

        # Resolve hostname to check target IPs
        try:
            addr_info = socket.getaddrinfo(
                hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
            for item in addr_info:
                sockaddr = item[4]
                ip_addr = sockaddr[0]
                if is_private_or_loopback_ip(ip_addr):
                    return (
                        False,
                        f"Host '{hostname}' resolved to private/loopback IP '{ip_addr}', forbidden by SSRF policy.",
                    )
        except socket.gaierror:
            # Name resolution failed - let HTTP client handle connection failure
            pass
        except Exception:
            pass

    # 3. Check allowed domains (if allowlist is populated)
    if policy.allowed_domains:
        allowed = any(
            is_domain_matching(hostname_lower, pattern) for pattern in policy.allowed_domains
        )
        if not allowed:
            return False, f"Domain '{hostname}' is not in the allowed domains list."

    return True, None


def check_outbound_url(url: str, policy: NetworkPolicy | None = None) -> None:
    """Validate outbound URL, raising NetworkSecurityError if invalid."""
    ok, error = validate_outbound_url(url, policy)
    if not ok:
        raise NetworkSecurityError(error or "Outbound request disallowed.")
