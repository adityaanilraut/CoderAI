"""Deterministic Tool Result Sanitizer & Credential Anonymizer for CoderAI.

Scans and redacts sensitive credentials (API keys, tokens, SSH keys, passwords, connection strings)
from tool outputs before they enter conversation history or LLM context.
"""

from __future__ import annotations

import re
from typing import Any

# Pre-compiled high-performance regex patterns
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # Private SSH / RSA / EC / DSA / OpenSSH / PGP Keys
    (
        "ssh_private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END (?:[A-Z0-9_-]+ )?PRIVATE KEY(?: BLOCK)?-----",
            re.MULTILINE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Anthropic & OpenAI API keys
    (
        "anthropic_api_key",
        re.compile(r"sk-ant-[A-Za-z0-9_-]{24,}", re.ASCII),
        "[REDACTED_ANTHROPIC_KEY]",
    ),
    (
        "openai_api_key",
        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}", re.ASCII),
        "[REDACTED_OPENAI_KEY]",
    ),
    # GitHub Tokens
    (
        "github_token",
        re.compile(
            r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{22,}", re.ASCII
        ),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # AWS Access Keys & Secrets
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", re.ASCII),
        "[REDACTED_AWS_KEY_ID]",
    ),
    (
        "aws_secret_key",
        re.compile(
            r"(?i)(?:aws_secret_access_key|aws_session_token)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
        ),
        r"aws_secret_access_key=[REDACTED_AWS_SECRET]",
    ),
    # GCP Service Account Private Key (JSON snippet)
    (
        "gcp_service_account",
        re.compile(r'"private_key":\s*"-----BEGIN PRIVATE KEY[\s\S]*?-----END PRIVATE KEY[\\n]*"'),
        '"private_key": "[REDACTED_GCP_PRIVATE_KEY]"',
    ),
    # JWT Tokens
    (
        "jwt_token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\b"),
        "[REDACTED_JWT_TOKEN]",
    ),
    # Database URIs with credentials (PostgreSQL, MySQL, MongoDB, Redis, etc.)
    (
        "database_uri_password",
        re.compile(
            r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql):\/\/[^:\/\s]+:)(.*)(@[^/@:\s]+(?::[0-9]+)?(?:\/[^\s\"']*)?)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED_DB_PASSWORD]\3",
    ),
    # Bearer Authorization headers
    (
        "bearer_token",
        re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9_\-\.\~]{20,}"),
        r"\1[REDACTED_BEARER_TOKEN]",
    ),
    # Generic credential assignments (api_key=..., password=..., secret=...)
    (
        "generic_secret",
        re.compile(
            r"""(?i)(?:(?<=['"])|(?<=\b))(?:api[_-]?key|access[_-]?token|secret[_-]?key|auth[_-]?token|client[_-]?secret|password|passwd|private[_-]?key)\s*[:=]\s*['"]?([A-Za-z0-9_\-+/=]{16,})['"]?""",
        ),
        r"[REDACTED_CREDENTIAL]",
    ),
]


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """Sanitize secrets from a string and return (sanitized_text, detected_types)."""
    if not text or not isinstance(text, str):
        return text, []

    detected_types: list[str] = []
    sanitized = text

    for secret_type, pattern, replacement in PATTERNS:
        if pattern.search(sanitized):
            detected_types.append(secret_type)
            if "\\" in replacement:
                sanitized = pattern.sub(replacement, sanitized)
            else:
                sanitized = pattern.sub(replacement, sanitized)

    return sanitized, detected_types


def sanitize_tool_output(output: Any) -> Any:
    """Recursively sanitize strings, dictionaries, lists, and ToolResult instances."""
    if output is None:
        return None

    if isinstance(output, str):
        sanitized, _ = sanitize_text(output)
        return sanitized

    if isinstance(output, dict):
        return {k: sanitize_tool_output(v) for k, v in output.items()}

    if isinstance(output, list):
        return [sanitize_tool_output(item) for item in output]

    # ToolResult instance support
    if hasattr(output, "output") or hasattr(output, "error"):
        if hasattr(output, "output") and isinstance(output.output, str):
            output.output, _ = sanitize_text(output.output)
        elif hasattr(output, "output") and isinstance(output.output, (dict, list)):
            output.output = sanitize_tool_output(output.output)

        if hasattr(output, "error") and isinstance(output.error, str):
            output.error, _ = sanitize_text(output.error)

        if hasattr(output, "follow_up_messages") and output.follow_up_messages:
            output.follow_up_messages = sanitize_tool_output(output.follow_up_messages)

        if hasattr(output, "metadata") and output.metadata:
            output.metadata = sanitize_tool_output(output.metadata)

        return output

    return output
