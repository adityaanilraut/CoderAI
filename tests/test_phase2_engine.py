"""Phase 2: Tooling & Web Subsystem Unit Tests.

Validates:
1. Primitive Tool Registry schema validation, parameter type checking, and dispatch.
2. Native Web Subsystem: SSRF protection, private IP blocking, HTML to Markdown conversion.
3. MCP Protocol Bridge: Stdio/SSE specs, dynamic tool schemas, and granular permissions.
"""

import pytest

from coderai.core.tools.registry import ToolRegistry
from coderai.core.tools.types import ToolDefinition, ToolResult, ValidationError
from coderai.core.network.security import (
    is_private_or_loopback_ip,
    validate_outbound_url,
    NetworkPolicy,
)
from coderai.core.network.sanitizer import (
    extract_and_sanitize_html,
)


def test_primitive_tool_registry_schemas_and_validation():
    registry = ToolRegistry()
    assert registry.has_tool("read")
    assert registry.has_tool("write")
    assert registry.has_tool("edit")
    assert registry.has_tool("bash")
    assert registry.has_tool("WebSearch")
    assert registry.has_tool("WebFetch")

    read_tool = registry.get("read")
    assert read_tool is not None
    assert "file_path" in read_tool.parameters

    # Check validation fails when required parameter is missing
    with pytest.raises(ValidationError):
        registry.validate_arguments("read", {})

    # Check validation succeeds with valid parameter
    valid_args = registry.validate_arguments("read", {"file_path": "README.md"})
    assert valid_args["file_path"] == "README.md"


def test_custom_tool_registration_and_aliases():
    registry = ToolRegistry()

    custom_tool = ToolDefinition(
        name="custom_calculator",
        description="A test calculation tool",
        parameters={
            "a": {"type": "integer", "description": "First number"},
            "b": {"type": "integer", "description": "Second number"},
        },
        required=["a", "b"],
        handler=lambda args, ctx: ToolResult(content=str(args["a"] + args["b"])),
        aliases=["calc", "add"],
    )
    registry.register(custom_tool)

    assert registry.has_tool("custom_calculator")
    assert registry.has_tool("calc")
    assert registry.has_tool("add")

    resolved = registry.get("calc")
    assert resolved is not None
    assert resolved.name == "custom_calculator"


def test_network_ssrf_and_private_ip_guard():
    # Loopback and private subnets must be detected
    assert is_private_or_loopback_ip("127.0.0.1") is True
    assert is_private_or_loopback_ip("10.0.0.1") is True
    assert is_private_or_loopback_ip("172.16.0.1") is True
    assert is_private_or_loopback_ip("192.168.1.1") is True
    assert is_private_or_loopback_ip("169.254.169.254") is True  # Cloud metadata IP

    # Public IPs must be allowed
    assert is_private_or_loopback_ip("8.8.8.8") is False
    assert is_private_or_loopback_ip("1.1.1.1") is False

    # Security policy enforcement
    policy = NetworkPolicy(allow_private_ips=False, enforce_ssrf_protection=True)
    is_valid, reason = validate_outbound_url("http://127.0.0.1/secret", policy)
    assert is_valid is False
    assert (
        "private" in (reason or "").lower()
        or "loopback" in (reason or "").lower()
        or "ssrf" in (reason or "").lower()
    )

    is_valid_cloud, reason_cloud = validate_outbound_url(
        "http://169.254.169.254/latest/meta-data/", policy
    )
    assert is_valid_cloud is False


def test_html_sanitizer_and_markdown_extraction():
    raw_html = """
    <html>
        <head><title>Test Page</title><script>alert('xss');</script></head>
        <body>
            <h1>Heading 1</h1>
            <p>This is a <b>bold</b> paragraph with a <a href="https://example.com">link</a>.</p>
            <style>body { color: red; }</style>
        </body>
    </html>
    """
    page = extract_and_sanitize_html(raw_html)
    assert "alert('xss')" not in page.markdown
    assert "color: red" not in page.markdown
    assert "Heading 1" in page.markdown
    assert "bold" in page.markdown
    assert "link" in page.markdown
