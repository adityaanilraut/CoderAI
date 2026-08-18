"""Unit and policy tests for Network Security, SSRF Rails, and Permissions Gating."""

import pytest

from coderai.core.network.security import (
    NetworkPolicy,
    is_domain_matching,
    is_private_or_loopback_ip,
    validate_outbound_url,
)
from coderai.core.permissions import (
    append_project_permission_allows,
    describe_tool_permission_request,
    evaluate_permission_scopes,
)

pytestmark = pytest.mark.security


def test_private_and_loopback_ip_detection():
    # IPv4 loopback & private
    assert is_private_or_loopback_ip("127.0.0.1")
    assert is_private_or_loopback_ip("127.0.1.1")
    assert is_private_or_loopback_ip("10.0.0.1")
    assert is_private_or_loopback_ip("192.168.1.1")
    assert is_private_or_loopback_ip("172.16.0.1")
    assert is_private_or_loopback_ip("169.254.169.254")  # AWS/Cloud metadata service

    # IPv6 loopback & private
    assert is_private_or_loopback_ip("::1")
    assert is_private_or_loopback_ip("fe80::1")

    # Public IPs
    assert not is_private_or_loopback_ip("8.8.8.8")
    assert not is_private_or_loopback_ip("1.1.1.1")
    assert not is_private_or_loopback_ip("142.250.190.46")


def test_domain_pattern_matching():
    assert is_domain_matching("api.github.com", "*.github.com")
    assert is_domain_matching("github.com", "*.github.com")
    assert is_domain_matching("raw.githubusercontent.com", "*")
    assert not is_domain_matching("evil.com", "*.github.com")
    assert is_domain_matching("pypi.org", "pypi.org")


def test_ssrf_and_domain_policy_enforcement():
    policy = NetworkPolicy(
        blocked_domains=["malicious.com", "*.attacker.org"],
        enforce_ssrf_protection=True,
        allow_private_ips=False,
    )

    # 1. Blocked domain
    ok, err = validate_outbound_url("https://malicious.com/payload", policy)
    assert not ok
    assert "blocked by security policy" in (err or "")

    ok, err = validate_outbound_url("https://sub.attacker.org/test", policy)
    assert not ok
    assert "blocked by security policy" in (err or "")

    # 2. SSRF loopback & private IP literals
    ok, err = validate_outbound_url("http://127.0.0.1:8080/admin", policy)
    assert not ok
    assert "SSRF" in (err or "")

    ok, err = validate_outbound_url("http://169.254.169.254/latest/meta-data/", policy)
    assert not ok
    assert "SSRF" in (err or "")

    # 3. Invalid scheme
    ok, err = validate_outbound_url("ftp://ftp.example.com/file", policy)
    assert not ok
    assert "Unsupported URL scheme" in (err or "")

    # 4. Valid public URL
    ok, err = validate_outbound_url("https://api.github.com/repos", policy)
    assert ok


def test_domain_allowlist_restriction():
    policy = NetworkPolicy(
        allowed_domains=["*.github.com", "pypi.org"],
        enforce_ssrf_protection=True,
    )

    # Allowed
    ok, _ = validate_outbound_url("https://api.github.com", policy)
    assert ok
    ok, _ = validate_outbound_url("https://pypi.org/project/coderai", policy)
    assert ok

    # Disallowed because not in allowlist
    ok, err = validate_outbound_url("https://example.com", policy)
    assert not ok
    assert "not in the allowed domains list" in (err or "")


def test_permission_evaluation_scopes():
    # allowAll default mode
    settings = {
        "allow": [],
        "deny": ["network"],
        "ask": ["write-in-cwd"],
        "defaultMode": "allowAll",
    }

    assert evaluate_permission_scopes(["read-in-cwd"], settings) == "allow"
    assert evaluate_permission_scopes(["write-in-cwd"], settings) == "ask"
    assert evaluate_permission_scopes(["network"], settings) == "deny"

    # askAll default mode
    settings_ask_all = {"allow": ["read-in-cwd"], "deny": [], "ask": [], "defaultMode": "askAll"}
    assert evaluate_permission_scopes(["read-in-cwd"], settings_ask_all) == "allow"
    assert evaluate_permission_scopes(["write-in-cwd"], settings_ask_all) == "ask"


def test_describe_tool_permissions_web_and_mcp():
    # WebFetch
    tc_fetch = {
        "id": "tc_fetch_1",
        "function": {"name": "WebFetch", "arguments": '{"url": "https://example.com"}'},
    }
    req_fetch = describe_tool_permission_request(
        session_id="s1", project_root="/tmp", tool_call=tc_fetch
    )
    assert req_fetch["scopes"] == ["network"]

    # WebSearch
    tc_search = {
        "id": "tc_search_1",
        "function": {"name": "WebSearch", "arguments": '{"query": "hello"}'},
    }
    req_search = describe_tool_permission_request(
        session_id="s1", project_root="/tmp", tool_call=tc_search
    )
    assert req_search["scopes"] == ["network"]

    # MCP tool
    tc_mcp = {
        "id": "tc_mcp_1",
        "function": {"name": "mcp__github__create_issue", "arguments": "{}"},
    }
    req_mcp = describe_tool_permission_request(
        session_id="s1", project_root="/tmp", tool_call=tc_mcp
    )
    assert req_mcp["scopes"] == ["mcp"]


def test_append_project_permission_allows(tmp_path):
    project_root = str(tmp_path)
    append_project_permission_allows(project_root, ["network", "mcp"])

    from coderai.core.settings import read_project_settings

    settings = read_project_settings(project_root)
    assert "network" in settings["permissions"]["allow"]
    assert "mcp" in settings["permissions"]["allow"]
