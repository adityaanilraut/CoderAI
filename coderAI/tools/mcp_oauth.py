"""OAuth 2.1 client for authorization-protected MCP servers.

Implements the MCP authorization flow (spec rev. 2025-06-18) for remote
Streamable HTTP servers that respond with ``401 + WWW-Authenticate`` instead of
accepting a static token:

* RFC 9728 — OAuth 2.0 Protected Resource Metadata discovery
* RFC 8414 — Authorization Server Metadata discovery
* RFC 7591 — Dynamic Client Registration (used when the server exposes it)
* RFC 7636 — PKCE (S256) authorization-code grant via a loopback redirect
* refresh-token grant for silent re-authentication

Tokens, the registered client id/secret and the resolved endpoints are stored
per server in ``~/.coderAI/mcp_credentials.json`` with ``0600`` permissions —
the same atomic-write pattern used for ``config.json`` API keys. After one
interactive ``coderAI mcp login`` the access token is refreshed silently on
every later connection, so the link "always stays saved" until the grant is
revoked.
"""

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from coderAI.system.fsperms import atomic_write_json

logger = logging.getLogger(__name__)

# Network/interaction timeouts (seconds).
_HTTP_TIMEOUT = 15
_CALLBACK_TIMEOUT = 300
# Refresh a little before the real expiry so an in-flight request never races
# the token going stale.
_EXPIRY_SKEW = 60


class OAuthError(Exception):
    """Raised when any step of the MCP OAuth flow fails."""


def _require_https_endpoint(url: Optional[str], label: str) -> str:
    """Return *url* if it is an https (or loopback-http) endpoint, else raise.

    Every OAuth endpoint we POST secrets to — token, registration, revocation —
    and every discovery URL is server-advertised and therefore untrusted. Routing
    an authorization code, refresh token or bearer over cleartext to a non-loopback
    host would leak it, so we reject a plaintext downgrade before the request is
    made. Shares the single scheme gate with the MCP transport layer.
    """
    from coderAI.tools.mcp import validate_remote_mcp_url

    if not url:
        raise OAuthError(f"{label}: missing endpoint URL.")
    err = validate_remote_mcp_url(url)
    if err:
        raise OAuthError(f"{label}: {err}")
    return url


# ---------------------------------------------------------------------------
# Credential store (~/.coderAI/mcp_credentials.json, 0600)
# ---------------------------------------------------------------------------


def mcp_credentials_path() -> Path:
    """Path to the persisted OAuth credentials (``~/.coderAI/mcp_credentials.json``)."""
    from coderAI.system.config import config_manager

    return config_manager.config_dir / "mcp_credentials.json"


def load_mcp_credentials() -> Dict[str, Any]:
    """Read the credential store, tolerating a missing or corrupt file."""
    path = mcp_credentials_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Failed to read %s; treating as empty", path, exc_info=True)
        return {}


def save_mcp_credentials(data: Dict[str, Any]) -> None:
    """Atomically write the credential store with ``0600`` permissions.

    Mirrors ``mcp.save_mcp_servers`` / ``ConfigManager.save``: write a temp file
    in the same directory, ``chmod 600`` it, then ``os.replace`` into place so a
    crash can never leave a truncated (and silently-wiped) credential file.
    """
    path = mcp_credentials_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    atomic_write_json(path, data)


def get_credentials(name: str) -> Optional[Dict[str, Any]]:
    """Return the stored credential record for a server, or ``None``."""
    rec = load_mcp_credentials().get(name)
    return rec if isinstance(rec, dict) else None


def set_credentials(name: str, record: Dict[str, Any]) -> None:
    """Persist (overwrite) the credential record for a server."""
    data = load_mcp_credentials()
    data[name] = record
    save_mcp_credentials(data)


def delete_credentials(name: str) -> bool:
    """Remove a server's credential record. Returns ``True`` if one existed."""
    data = load_mcp_credentials()
    if name not in data:
        return False
    del data[name]
    save_mcp_credentials(data)
    return True


def has_credentials(name: str) -> bool:
    """Whether a (non-empty) credential record exists for a server."""
    return get_credentials(name) is not None


# ---------------------------------------------------------------------------
# PKCE + loopback helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_pkce() -> Tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` PKCE pair (S256)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _pick_loopback_port() -> int:
    """Grab a free TCP port on the loopback interface for the redirect URI."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler that records the OAuth redirect query string."""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)

        def first(key: str) -> Optional[str]:
            vals = params.get(key)
            return vals[0] if vals else None

        self.server.oauth_result = {  # type: ignore[attr-defined]
            "code": first("code"),
            "state": first("state"),
            "error": first("error"),
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif'>"
            b"<h2>CoderAI: authorization complete.</h2>"
            b"<p>You can close this tab and return to your terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args: Any) -> None:  # silence stdlib request logging
        return


def _capture_authorization_code(
    port: int, expected_state: str, auth_url: str, timeout: int = _CALLBACK_TIMEOUT
) -> str:
    """Open the browser to ``auth_url`` and block until the redirect returns a code.

    Validates ``state`` (CSRF) and surfaces user-denied authorizations. The
    loopback server is torn down as soon as the single callback is handled.
    """
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.oauth_result = None  # type: ignore[attr-defined]
    server.timeout = 1

    def _serve() -> None:
        deadline = time.monotonic() + timeout
        while getattr(server, "oauth_result", None) is None and time.monotonic() < deadline:
            server.handle_request()

    worker = threading.Thread(target=_serve, daemon=True)
    worker.start()
    if not webbrowser.open(auth_url):
        logger.info("Could not open a browser automatically; visit:\n%s", auth_url)
    worker.join(timeout + 5)

    result = getattr(server, "oauth_result", None)
    try:
        server.server_close()
    except Exception:
        pass

    if not result:
        raise OAuthError("Timed out waiting for the authorization callback.")
    if result.get("error"):
        raise OAuthError(f"Authorization was denied: {result['error']}")
    if result.get("state") != expected_state:
        raise OAuthError("State mismatch on callback — possible CSRF; aborting.")
    if not result.get("code"):
        raise OAuthError("Authorization callback returned no code.")
    return str(result["code"])


# ---------------------------------------------------------------------------
# Discovery (RFC 9728 / RFC 8414)
# ---------------------------------------------------------------------------


_MULTI_LABEL_SUFFIXES = {"co", "com", "org", "net", "gov", "edu", "ac"}


def _registrable_domain(host: str) -> str:
    """Best-effort registrable domain (eTLD+1) for *host*, no PSL dependency.

    Returns the last two labels, or the last three when the second-to-last label
    is a common two-level public suffix (``co.uk``, ``com.au``). IP literals and
    already-short hosts are returned unchanged. Only used to decide whether to
    *warn* about an auth-server/MCP-server domain mismatch, so an approximation is
    acceptable — a false "differs" warning is safe (it just prompts scrutiny).
    """
    host = (host or "").strip().lower().strip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if labels[-2] in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def authorization_origin_warning(server_url: str, authorization_endpoint: str) -> Optional[str]:
    """Warn when the auth server's domain differs from the MCP server's (7.4).

    The authorization endpoint comes from *server-advertised* metadata, so a
    malicious MCP server could point the browser (and the user's credentials) at
    an attacker-controlled issuer. PKCE+state protect the code exchange, but the
    human should still see, and be able to reject, an unexpected login origin.
    Returns a warning string on a registrable-domain mismatch, else ``None``.
    """
    as_host = urllib.parse.urlparse(authorization_endpoint).hostname or ""
    mcp_host = urllib.parse.urlparse(server_url).hostname or ""
    if not as_host or _registrable_domain(as_host) == _registrable_domain(mcp_host):
        return None
    return (
        f"authorization server '{as_host}' is a different domain than the MCP "
        f"server '{mcp_host}' — only continue if you trust it."
    )


def _announce_authorization_origin(server_url: str, authorization_endpoint: str) -> None:
    """Show the auth-server origin (and any domain-mismatch warning) pre-browser."""
    parsed = urllib.parse.urlparse(authorization_endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    warning = authorization_origin_warning(server_url, authorization_endpoint)
    try:
        from coderAI.cli.utils import display

        display.print_info(f"Authorizing at {origin}")
        if warning:
            display.print_warning("⚠  " + warning)
    except Exception:
        logger.info("Authorizing at %s", origin)
    if warning:
        logger.warning("MCP OAuth origin mismatch: %s", warning)


def parse_www_authenticate_resource(header: Optional[str]) -> Optional[str]:
    """Extract ``resource_metadata="…"`` from a ``WWW-Authenticate`` header."""
    if not header:
        return None
    for part in header.replace(",", " ").split():
        if part.startswith("resource_metadata="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


def _well_known(origin: str, suffix: str) -> str:
    parsed = urllib.parse.urlparse(origin)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/{suffix}"


def discover_protected_resource(
    server_url: str, www_authenticate: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch the protected-resource metadata document (RFC 9728)."""
    rm_url = parse_www_authenticate_resource(www_authenticate) or _well_known(
        server_url, "oauth-protected-resource"
    )
    _require_https_endpoint(rm_url, "protected-resource metadata")
    resp = requests.get(rm_url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise OAuthError(f"Malformed protected-resource metadata at {rm_url}")
    return data


def discover_auth_server(issuer: str) -> Dict[str, Any]:
    """Fetch authorization-server metadata for an issuer (RFC 8414 / OIDC).

    Tries the RFC 8414 path-insertion form first (which is what Strava and most
    multi-tenant issuers use), then the plain issuer-suffixed and OIDC forms.
    """
    _require_https_endpoint(issuer, "authorization-server issuer")
    parsed = urllib.parse.urlparse(issuer)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [
        f"{base}/.well-known/oauth-authorization-server{path}",
        f"{base}/.well-known/openid-configuration{path}",
        f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
    ]
    last_err: Optional[Exception] = None
    for url in candidates:
        try:
            resp = requests.get(url, timeout=_HTTP_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if isinstance(data, dict) and data.get("token_endpoint"):
                return data
        except Exception as e:  # try the next candidate
            last_err = e
    raise OAuthError(
        f"Could not locate authorization-server metadata for issuer {issuer!r}"
        + (f" ({last_err})" if last_err else "")
    )


def discover_metadata(
    server_url: str, www_authenticate: Optional[str] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return ``(protected_resource_metadata, authorization_server_metadata)``."""
    rm = discover_protected_resource(server_url, www_authenticate)
    auth_servers = rm.get("authorization_servers") or []
    if not auth_servers:
        raise OAuthError("Protected-resource metadata lists no authorization_servers.")
    as_meta = discover_auth_server(auth_servers[0])
    return rm, as_meta


def probe_www_authenticate(server_url: str) -> Optional[str]:
    """POST a minimal ``initialize`` to detect the ``WWW-Authenticate`` challenge."""
    from coderAI.tools.mcp import validate_remote_mcp_url

    # Best-effort probe: never POST to a plaintext (non-loopback) server; the
    # crisp rejection is raised by ``login`` / discovery instead.
    if validate_remote_mcp_url(server_url):
        return None
    try:
        resp = requests.post(
            server_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            return resp.headers.get("WWW-Authenticate")
    except Exception:
        logger.debug("WWW-Authenticate probe failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Registration + token grants
# ---------------------------------------------------------------------------


def register_client(
    as_meta: Dict[str, Any], redirect_uri: str, client_name: str = "CoderAI"
) -> Optional[Dict[str, Any]]:
    """Dynamically register a public OAuth client (RFC 7591), if supported.

    Returns the registration response (``client_id`` and optionally
    ``client_secret``), or ``None`` when the server exposes no registration
    endpoint — in which case the caller must supply a pre-issued client id.
    """
    endpoint = as_meta.get("registration_endpoint")
    if not endpoint:
        return None
    _require_https_endpoint(endpoint, "registration endpoint")
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    resp = requests.post(endpoint, json=body, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("client_id"):
        raise OAuthError("Dynamic client registration returned no client_id.")
    return data


def _token_request(token_endpoint: str, data: Dict[str, Optional[str]]) -> Dict[str, Any]:
    _require_https_endpoint(token_endpoint, "token endpoint")
    resp = requests.post(
        token_endpoint,
        data={k: v for k, v in data.items() if v is not None},
        headers={"Accept": "application/json"},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise OAuthError(f"Token endpoint returned HTTP {resp.status_code}: {resp.text[:300]}")
    token = resp.json()
    if not isinstance(token, dict) or "access_token" not in token:
        raise OAuthError("Token response did not contain an access_token.")
    return token


def exchange_code(
    as_meta: Dict[str, Any],
    *,
    client_id: str,
    client_secret: Optional[str],
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: Optional[str],
) -> Dict[str, Any]:
    """Exchange an authorization code for tokens (authorization_code grant)."""
    return _token_request(
        as_meta["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,  # filtered out if None
            "code_verifier": code_verifier,
            "resource": resource,
        },
    )


def refresh_token_grant(record: Dict[str, Any]) -> Dict[str, Any]:
    """Exchange a stored refresh token for a fresh access token."""
    refresh = record.get("refresh_token")
    if not refresh:
        raise OAuthError("No refresh_token stored; interactive login required.")
    return _token_request(
        record["token_endpoint"],
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": record.get("client_id"),
            "client_secret": record.get("client_secret"),
            "resource": record.get("resource"),
        },
    )


def _apply_token(record: Dict[str, Any], token: Dict[str, Any]) -> None:
    """Merge a token response into a credential record (preserving refresh token)."""
    record["access_token"] = token["access_token"]
    # Refresh tokens are often returned only on first issue; keep the old one.
    if token.get("refresh_token"):
        record["refresh_token"] = token["refresh_token"]
    expires_in = token.get("expires_in")
    record["expires_at"] = time.time() + float(expires_in) if expires_in else time.time() + 3600
    if token.get("scope"):
        record["scope"] = token["scope"]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def login(
    name: str,
    server_url: str,
    *,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    scopes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the interactive OAuth login for a server and persist its credentials.

    Discovers metadata, registers (or reuses) a client, opens the browser for
    the PKCE authorization-code flow, exchanges the code, and saves the full
    credential record. Returns that record.
    """
    _require_https_endpoint(server_url, "MCP server")
    www_auth = probe_www_authenticate(server_url)
    rm, as_meta = discover_metadata(server_url, www_auth)
    resource = rm.get("resource") or server_url
    if scopes is None:
        scopes = rm.get("scopes_supported") or None

    port = _pick_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    existing = get_credentials(name) or {}
    secret = client_secret
    if not client_id:
        client_id = existing.get("client_id")
        secret = secret or existing.get("client_secret")
    if not client_id:
        reg = register_client(as_meta, redirect_uri)
        if not reg:
            raise OAuthError(
                "Server has no dynamic registration endpoint; pass --client-id "
                "(and --client-secret if confidential)."
            )
        client_id = reg["client_id"]
        secret = reg.get("client_secret")

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": resource,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    authorization_endpoint = _require_https_endpoint(
        as_meta.get("authorization_endpoint"), "authorization endpoint"
    )
    auth_url = authorization_endpoint + "?" + urllib.parse.urlencode(params)

    _announce_authorization_origin(server_url, authorization_endpoint)
    code = _capture_authorization_code(port, state, auth_url)
    token = exchange_code(
        as_meta,
        client_id=client_id,
        client_secret=secret,
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        resource=resource,
    )

    record: Dict[str, Any] = {
        "issuer": as_meta.get("issuer"),
        "token_endpoint": as_meta["token_endpoint"],
        "authorization_endpoint": as_meta.get("authorization_endpoint"),
        "registration_endpoint": as_meta.get("registration_endpoint"),
        "revocation_endpoint": as_meta.get("revocation_endpoint"),
        "client_id": client_id,
        "resource": resource,
    }
    if secret:
        record["client_secret"] = secret
    _apply_token(record, token)
    set_credentials(name, record)
    return record


def get_valid_token_sync(name: str) -> Optional[str]:
    """Return a currently-valid access token for a server, refreshing if needed.

    Silent only: returns ``None`` (never opens a browser) when there are no
    stored credentials or the refresh fails — the caller decides whether to
    prompt for an interactive ``mcp login``. Safe to call from an executor via
    ``asyncio.to_thread`` so it never blocks the event loop.
    """
    record = get_credentials(name)
    if not record:
        return None

    access = record.get("access_token")
    expires_at = record.get("expires_at", 0)
    if access and time.time() < float(expires_at) - _EXPIRY_SKEW:
        return str(access)

    if not record.get("refresh_token"):
        return None
    try:
        token = refresh_token_grant(record)
    except Exception:
        logger.warning(
            "Silent token refresh failed for MCP server %r; run `coderAI mcp login %s`",
            name,
            name,
        )
        return None
    _apply_token(record, token)
    set_credentials(name, record)
    return str(record["access_token"])


def logout(name: str) -> bool:
    """Best-effort revoke the refresh token, then delete the stored credentials.

    Returns ``True`` if credentials existed. A failed revocation does not block
    deletion — the local copy is removed regardless.
    """
    record = get_credentials(name)
    if not record:
        return False
    from coderAI.tools.mcp import validate_remote_mcp_url

    revoke = record.get("revocation_endpoint")
    refresh = record.get("refresh_token")
    # Best-effort revocation: never raise, and never POST the refresh token to a
    # plaintext (non-loopback) endpoint — a poisoned record could exfiltrate it.
    if revoke and refresh and validate_remote_mcp_url(revoke) is None:
        try:
            requests.post(
                revoke,
                data={
                    "token": refresh,
                    "token_type_hint": "refresh_token",
                    "client_id": record.get("client_id"),
                    "client_secret": record.get("client_secret"),
                },
                timeout=_HTTP_TIMEOUT,
            )
        except Exception:
            logger.debug("Token revocation request failed", exc_info=True)
    delete_credentials(name)
    return True
