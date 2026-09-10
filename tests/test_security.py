"""Consolidated security rails: SSRF policy, permissions, sandbox, and secrets."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from coderai.cli.doctor import mask_secret
from coderai.core.network.security import (
    NetworkPolicy,
    is_domain_matching,
    is_private_or_loopback_ip,
    validate_outbound_url,
)
from coderai.core.permissions import (
    PLAN_MODE_FORCE_ASK_SCOPES,
    append_project_permission_allows,
    compute_tool_call_permissions,
    describe_tool_permission_request,
    evaluate_permission_scopes,
)
from coderai.core.sandbox import (
    build_seatbelt_profile,
    check_sandbox_path_access,
    parse_sandbox_mode,
    preset_permissions,
    sandbox_policy_prompt,
    wrap_sandbox_command,
)
from coderai.core.session import sanitize_repetition_loops
from coderai.core.settings import read_project_settings
from coderai.core.network.client import HttpClient
from coderai.tools.file.utils import is_dry_run

pytestmark = pytest.mark.security


def test_security_ssrf_blocks_private_ip():
    """Private, loopback, and metadata IPs are detected as non-routable."""
    assert is_private_or_loopback_ip("127.0.0.1")
    assert is_private_or_loopback_ip("10.0.0.1")
    assert is_private_or_loopback_ip("192.168.1.1")
    assert is_private_or_loopback_ip("172.16.0.1")
    assert is_private_or_loopback_ip("169.254.169.254")
    assert is_private_or_loopback_ip("::1")
    assert is_private_or_loopback_ip("fe80::1")
    assert not is_private_or_loopback_ip("8.8.8.8")
    assert not is_private_or_loopback_ip("1.1.1.1")


def test_security_ssrf_rejects_private_url():
    """Outbound URLs resolving to private IPs fail with an SSRF error."""
    policy = NetworkPolicy(enforce_ssrf_protection=True, allow_private_ips=False)
    ok, err = validate_outbound_url("http://127.0.0.1:8080/admin", policy)
    assert not ok
    assert "SSRF" in (err or "")
    ok, err = validate_outbound_url("http://169.254.169.254/latest/meta-data/", policy)
    assert not ok
    assert "SSRF" in (err or "")
    ok, _ = validate_outbound_url("https://api.github.com/repos", policy)
    assert ok


def test_security_domain_matches_wildcard_pattern():
    """Wildcard and exact domain patterns match as expected."""
    assert is_domain_matching("api.github.com", "*.github.com")
    assert is_domain_matching("github.com", "*.github.com")
    assert is_domain_matching("raw.githubusercontent.com", "*")
    assert is_domain_matching("pypi.org", "pypi.org")
    assert not is_domain_matching("evil.com", "*.github.com")


def test_security_domain_blocks_denied_pattern():
    """Explicitly blocked domains and wildcard patterns are rejected."""
    policy = NetworkPolicy(
        blocked_domains=["malicious.com", "*.attacker.org"],
        enforce_ssrf_protection=True,
    )
    ok, err = validate_outbound_url("https://malicious.com/payload", policy)
    assert not ok
    assert "blocked by security policy" in (err or "")
    ok, err = validate_outbound_url("https://sub.attacker.org/test", policy)
    assert not ok
    assert "blocked by security policy" in (err or "")


def test_security_domain_rejects_unlisted_allow():
    """Allowlisted policy permits listed domains and rejects everything else."""
    policy = NetworkPolicy(
        allowed_domains=["*.github.com", "pypi.org"], enforce_ssrf_protection=True
    )
    ok, _ = validate_outbound_url("https://api.github.com", policy)
    assert ok
    ok, _ = validate_outbound_url("https://pypi.org/project/coderai", policy)
    assert ok
    ok, err = validate_outbound_url("https://example.com", policy)
    assert not ok
    assert "not in the allowed domains list" in (err or "")


def test_security_url_rejects_unsupported_scheme():
    """Non-HTTP(S) schemes are rejected by outbound validation."""
    ok, err = validate_outbound_url("ftp://ftp.example.com/file", NetworkPolicy())
    assert not ok
    assert "Unsupported URL scheme" in (err or "")


def test_security_redirect_blocks_private_target():
    """POST redirects escaping the policy to a metadata IP are blocked."""
    client = HttpClient()
    client.policy = NetworkPolicy(allowed_domains=["example.com"])
    redirect = MagicMock(
        is_redirect=True,
        is_permanent_redirect=False,
        status_code=302,
        headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )
    with patch.object(client._session, "post", return_value=redirect):
        res = client.post("https://example.com/api")
        assert res.ok is False
        assert "blocked" in (res.error or "").lower() or "forbidden" in (res.error or "").lower()


def test_security_dns_fails_closed():
    """DNS resolution crashes fail closed instead of permitting the request."""
    with patch("socket.getaddrinfo", side_effect=RuntimeError("DNS crashed")):
        ok, err = validate_outbound_url("https://example.com", NetworkPolicy(allowed_domains=[]))
        assert ok is False
        assert "failed to resolve host" in (err or "").lower()


def test_security_permissions_evaluate_scopes():
    """Scope lists resolve to allow/ask/deny under both default modes."""
    allow_all = {
        "allow": [],
        "deny": ["network"],
        "ask": ["write-in-cwd"],
        "defaultMode": "allowAll",
    }
    assert evaluate_permission_scopes(["read-in-cwd"], allow_all) == "allow"
    assert evaluate_permission_scopes(["write-in-cwd"], allow_all) == "ask"
    assert evaluate_permission_scopes(["network"], allow_all) == "deny"
    ask_all = {"allow": ["read-in-cwd"], "deny": [], "ask": [], "defaultMode": "askAll"}
    assert evaluate_permission_scopes(["read-in-cwd"], ask_all) == "allow"
    assert evaluate_permission_scopes(["write-in-cwd"], ask_all) == "ask"


def test_security_permissions_describe_network_and_mcp():
    """Web tools map to the network scope and namespaced tools to mcp."""
    for name, args, scope in [
        ("WebFetch", '{"url": "https://example.com"}', "network"),
        ("WebSearch", '{"query": "hello"}', "network"),
        ("mcp__github__create_issue", "{}", "mcp"),
    ]:
        req = describe_tool_permission_request(
            session_id="s1",
            project_root="/tmp",
            tool_call={"id": "tc1", "function": {"name": name, "arguments": args}},
        )
        assert req["scopes"] == [scope]


def test_security_permissions_append_project_allow(tmp_path):
    """Project allowlist additions persist to project settings."""
    append_project_permission_allows(str(tmp_path), ["network", "mcp"])
    settings = read_project_settings(str(tmp_path))
    assert "network" in settings["permissions"]["allow"]
    assert "mcp" in settings["permissions"]["allow"]


def test_security_permissions_plan_mode_forces_ask():
    """Plan-mode force-ask scopes escalate writes to an approval prompt."""
    res = compute_tool_call_permissions(
        session_id="s_plan",
        project_root="/tmp/test_project",
        tool_calls=[
            {
                "id": "tc1",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": '{"file_path": "foo.py", "content": "..."}',
                },
            }
        ],
        settings={"defaultMode": "allowAll"},
        force_ask_scopes=PLAN_MODE_FORCE_ASK_SCOPES,
    )
    assert res["askPermissions"] is not None
    assert len(res["askPermissions"]) > 0


def test_security_sandbox_presets_define_scopes():
    """Sandbox preset names parse and each preset gates the expected scopes."""
    assert parse_sandbox_mode("readonly") == "read-only"
    assert parse_sandbox_mode("workspace_write") == "workspace-write"
    assert parse_sandbox_mode("none") == "danger-full-access"
    ro = preset_permissions("read-only")
    assert "write-in-cwd" in ro["deny"]
    assert "read-in-cwd" in ro["allow"]
    ww = preset_permissions("workspace-write")
    assert "write-in-cwd" in ww["allow"]
    assert "write-out-cwd" in ww["deny"]
    dfa = preset_permissions("danger-full-access")
    assert len(dfa["deny"]) == 0
    assert dfa["defaultMode"] == "allowAll"
    assert "read-only" in sandbox_policy_prompt("read-only")


def test_security_sandbox_seatbelt_permits_posix_devices(tmp_path):
    """Generated Seatbelt profiles keep standard POSIX terminal devices writable."""
    profile = build_seatbelt_profile("workspace-write", str(tmp_path))
    assert "(version 1)" in profile
    assert "(deny default)" in profile
    for device in (
        "/dev/null",
        "/dev/zero",
        "/dev/urandom",
        "/dev/random",
        "/dev/tty",
        "/dev/ptmx",
        "/dev/stdout",
        "/dev/stderr",
    ):
        assert f'(allow file-write-data (literal "{device}"))' in profile


def test_security_sandbox_confines_workspace_write(tmp_path):
    """Workspace-write mode allows inside writes and blocks outside writes."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ok_in, err_in = check_sandbox_path_access(
        ws / "test.txt", op="write", mode="workspace-write", workspace_root=ws
    )
    assert ok_in is True
    assert err_in is None
    ok_out, err_out = check_sandbox_path_access(
        tmp_path / "outside.txt", op="write", mode="workspace-write", workspace_root=ws
    )
    assert ok_out is False
    assert "SANDBOX_VIOLATION" in (err_out or "")
    ok_ro, err_ro = check_sandbox_path_access(
        ws / "test.txt", op="write", mode="read-only", workspace_root=ws
    )
    assert ok_ro is False
    assert "SANDBOX_VIOLATION" in (err_ro or "")


def test_security_sandbox_wrapping_skips_full_access():
    """Danger-full-access mode leaves the command argv untouched."""
    argv = ["echo", "hello"]
    wrapped, meta = wrap_sandbox_command(
        argv, mode="danger-full-access", workspace_root="/tmp/project"
    )
    assert wrapped == argv
    assert meta["sandboxApplied"] is False


def test_security_dryrun_detects_preview_mode():
    """Dry-run is detected from the context or the tool arguments."""
    assert is_dry_run(SimpleNamespace(dry_run=True), None) is True
    assert is_dry_run(SimpleNamespace(), {"dry_run": True}) is True
    assert is_dry_run(SimpleNamespace(), None) is False
    assert is_dry_run(SimpleNamespace(dry_run=False), {}) is False


def test_security_credentials_mask_secrets():
    """Credential display keeps a hint prefix while hiding the secret body."""
    assert mask_secret("sk-abcdef1234567890") == "sk-a...890"
    assert mask_secret("ab") == "****"
    assert mask_secret(None) == "Not Set"


def test_security_repetition_collapses_degenerate_loop():
    """Degenerate repeated model output is collapsed while normal text passes through."""
    normal = "This is a standard assistant reply explaining code."
    assert sanitize_repetition_loops(normal) == normal
    degenerate = "Here is the code:\n" + ("function test() { return 1; }\n" * 30)
    assert len(sanitize_repetition_loops(degenerate)) < len(degenerate)
