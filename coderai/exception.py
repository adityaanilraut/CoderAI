# Ported from coderai/core/errors.py - kimi structure (exception.py).
"""Canonical exception hierarchy (Kimi ``exception.py`` parity).

Single import point for typed errors so call sites don't scatter bare
``ValueError``/``RuntimeError`` subclasses. Mirrors Kimi's hierarchy 1:1.
"""

from __future__ import annotations


class CoderAIException(Exception):
    """Base exception class for CoderAI."""

    pass


class ConfigError(CoderAIException, ValueError):
    """Configuration error."""

    pass


class AgentSpecError(CoderAIException, ValueError):
    """Agent specification error."""

    pass


class InvalidToolError(CoderAIException, ValueError):
    """Invalid tool error."""

    pass


class SystemPromptTemplateError(CoderAIException, ValueError):
    """System prompt template error."""

    pass


class MCPConfigError(CoderAIException, ValueError):
    """MCP config error."""

    pass


class MCPRuntimeError(CoderAIException, RuntimeError):
    """MCP runtime error."""

    pass
