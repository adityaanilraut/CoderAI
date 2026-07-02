"""Tests for the MCP OAuth client (coderAI/tools/mcp_oauth.py)."""

import os
import stat
import time

import pytest
from unittest.mock import patch

import coderAI.tools.mcp_oauth as oauth


@pytest.fixture
def creds_dir(tmp_path, monkeypatch):
    """Point the credential store at a temp dir (resolved via config_dir)."""
    monkeypatch.setattr("coderAI.system.config.config_manager.config_dir", tmp_path)
    return tmp_path


class _FakeResp:
    def __init__(self, status=200, json_data=None, headers=None, text=""):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── credential store ───────────────────────────────────────────────────────


def test_credentials_round_trip(creds_dir):
    oauth.set_credentials("srv", {"access_token": "a", "refresh_token": "r"})
    assert oauth.get_credentials("srv") == {"access_token": "a", "refresh_token": "r"}
    assert oauth.has_credentials("srv") is True
    assert oauth.has_credentials("ghost") is False


def test_credentials_file_is_0600(creds_dir):
    oauth.set_credentials("srv", {"access_token": "a"})
    mode = stat.S_IMODE(os.stat(oauth.mcp_credentials_path()).st_mode)
    assert mode == 0o600


def test_delete_credentials(creds_dir):
    oauth.set_credentials("srv", {"access_token": "a"})
    assert oauth.delete_credentials("srv") is True
    assert oauth.delete_credentials("srv") is False
    assert oauth.get_credentials("srv") is None


def test_corrupt_store_treated_as_empty(creds_dir):
    oauth.mcp_credentials_path().write_text("{not json")
    assert oauth.load_mcp_credentials() == {}


# ── discovery helpers ────────────────────────────────────────────────────────


def test_parse_www_authenticate_resource():
    hdr = 'Bearer error="invalid_token", resource_metadata="https://h/.well-known/x"'
    assert oauth.parse_www_authenticate_resource(hdr) == "https://h/.well-known/x"
    assert oauth.parse_www_authenticate_resource(None) is None
    assert oauth.parse_www_authenticate_resource("Bearer realm=x") is None


def test_discover_auth_server_prefers_path_insertion(creds_dir):
    seen = []

    def fake_get(url, timeout=None):
        seen.append(url)
        if url == "https://www.x.com/.well-known/oauth-authorization-server/iss":
            return _FakeResp(200, {"token_endpoint": "https://www.x.com/tok"})
        return _FakeResp(404)

    with patch.object(oauth.requests, "get", side_effect=fake_get):
        meta = oauth.discover_auth_server("https://www.x.com/iss")

    assert meta["token_endpoint"] == "https://www.x.com/tok"
    assert seen[0] == "https://www.x.com/.well-known/oauth-authorization-server/iss"


def test_discover_metadata_requires_auth_servers(creds_dir):
    with patch.object(oauth, "discover_protected_resource", return_value={"resource": "u"}):
        with pytest.raises(oauth.OAuthError):
            oauth.discover_metadata("https://h/mcp")


# ── token grants + silent refresh ────────────────────────────────────────────


def test_get_valid_token_returns_unexpired(creds_dir):
    oauth.set_credentials(
        "srv", {"access_token": "live", "expires_at": time.time() + 600}
    )
    assert oauth.get_valid_token_sync("srv") == "live"


def test_get_valid_token_none_without_creds(creds_dir):
    assert oauth.get_valid_token_sync("srv") is None


def test_get_valid_token_refreshes_when_expired(creds_dir):
    oauth.set_credentials(
        "srv",
        {
            "access_token": "old",
            "refresh_token": "r1",
            "expires_at": time.time() - 10,
            "token_endpoint": "https://h/tok",
            "client_id": "cid",
        },
    )

    def fake_post(url, data=None, headers=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "r1"
        return _FakeResp(200, {"access_token": "new", "expires_in": 3600})

    with patch.object(oauth.requests, "post", side_effect=fake_post):
        token = oauth.get_valid_token_sync("srv")

    assert token == "new"
    # The refreshed token must be persisted for next time.
    assert oauth.get_credentials("srv")["access_token"] == "new"


def test_get_valid_token_none_when_expired_and_no_refresh(creds_dir):
    oauth.set_credentials("srv", {"access_token": "old", "expires_at": time.time() - 10})
    assert oauth.get_valid_token_sync("srv") is None


def test_get_valid_token_none_when_refresh_fails(creds_dir):
    oauth.set_credentials(
        "srv",
        {
            "access_token": "old",
            "refresh_token": "r1",
            "expires_at": time.time() - 10,
            "token_endpoint": "https://h/tok",
        },
    )
    with patch.object(oauth.requests, "post", side_effect=RuntimeError("boom")):
        assert oauth.get_valid_token_sync("srv") is None


# ── interactive login orchestration ──────────────────────────────────────────


@pytest.mark.enable_socket  # login() binds a loopback ephemeral port for the callback server
def test_login_happy_path_persists_tokens(creds_dir):
    rm = {
        "resource": "https://mcp.h/mcp",
        "scopes_supported": ["read"],
        "authorization_servers": ["https://h/iss"],
    }
    as_meta = {
        "issuer": "https://h",
        "authorization_endpoint": "https://h/authorize",
        "token_endpoint": "https://h/token",
        "registration_endpoint": "https://h/register",
        "revocation_endpoint": "https://h/revoke",
    }
    with patch.object(oauth, "probe_www_authenticate", return_value=None), patch.object(
        oauth, "discover_metadata", return_value=(rm, as_meta)
    ), patch.object(
        oauth, "register_client", return_value={"client_id": "cid"}
    ), patch.object(
        oauth, "_capture_authorization_code", return_value="code123"
    ), patch.object(
        oauth,
        "exchange_code",
        return_value={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600},
    ) as exch:
        record = oauth.login("strava", "https://mcp.h/mcp")

    assert record["access_token"] == "AT"
    assert record["resource"] == "https://mcp.h/mcp"
    # Persisted for silent reuse.
    saved = oauth.get_credentials("strava")
    assert saved["refresh_token"] == "RT"
    assert saved["client_id"] == "cid"
    # PKCE verifier + resource were passed to the token exchange.
    kwargs = exch.call_args.kwargs
    assert kwargs["code"] == "code123"
    assert kwargs["resource"] == "https://mcp.h/mcp"
    assert kwargs["code_verifier"]


@pytest.mark.enable_socket  # login() binds a loopback ephemeral port for the callback server
def test_login_errors_without_registration_or_client_id(creds_dir):
    rm = {"resource": "u", "authorization_servers": ["https://h/iss"]}
    as_meta = {"authorization_endpoint": "https://h/a", "token_endpoint": "https://h/t"}
    with patch.object(oauth, "probe_www_authenticate", return_value=None), patch.object(
        oauth, "discover_metadata", return_value=(rm, as_meta)
    ), patch.object(oauth, "register_client", return_value=None):
        with pytest.raises(oauth.OAuthError):
            oauth.login("srv", "https://h/mcp")


def test_logout_revokes_and_deletes(creds_dir):
    oauth.set_credentials(
        "srv",
        {"refresh_token": "r1", "revocation_endpoint": "https://h/revoke", "client_id": "c"},
    )
    with patch.object(oauth.requests, "post", return_value=_FakeResp(200)) as post:
        assert oauth.logout("srv") is True

    post.assert_called_once()
    assert oauth.get_credentials("srv") is None
    assert oauth.logout("srv") is False


# ── PKCE ─────────────────────────────────────────────────────────────────────


def test_generate_pkce_is_url_safe_and_distinct():
    verifier, challenge = oauth.generate_pkce()
    assert verifier and challenge and verifier != challenge
    assert "=" not in verifier and "=" not in challenge
    assert "+" not in challenge and "/" not in challenge
