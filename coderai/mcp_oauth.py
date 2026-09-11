# Ported from coderai/core/mcp/oauth.py - kimi structure (kimi_cli/mcp_oauth.py).
"""Bearer-token auth for OAuth-protected MCP servers (Kimi ``mcp_oauth`` parity, slim).

No browser flow is vendored (and no ``fastmcp`` dependency): servers with
``"auth": "oauth"`` authenticate with a pre-obtained bearer token resolved
from, in order:

1. ``token`` — inline token (discouraged; prefer the options below)
2. ``tokenEnv`` — name of an environment variable holding the token
3. ``tokenFile`` — path to a file holding the token (or a ``.json`` document
   with an ``access_token`` field)
4. ``~/.coderai/mcp-oauth/<server>.json`` — per-server token file written by
   ``coderai mcp login`` (same ``{"access_token": ...}`` shape)

A full interactive OAuth dance is follow-up work; static tokens cover the
common case (dashboard-issued or CLI-provisioned tokens).
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from coderai.share import get_share_dir


def oauth_token_dir() -> Path:
    path = get_share_dir() / "mcp-oauth"
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    return path


def server_token_path(server_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in server_name)
    return oauth_token_dir() / f"{safe or 'server'}.json"


def _token_from_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if isinstance(payload, dict):
            for key in ("access_token", "token", "bearer"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None
    return text.split()[0]


def resolve_server_bearer_token(
    config: dict[str, Any], server_name: str
) -> str | None:
    """Resolve the bearer token for an ``auth: oauth`` server config."""
    inline = config.get("token")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    env_name = config.get("tokenEnv")
    if isinstance(env_name, str) and env_name.strip():
        value = os.getenv(env_name.strip())
        if value and value.strip():
            return value.strip()
    token_file = config.get("tokenFile")
    if isinstance(token_file, str) and token_file.strip():
        candidate = Path(token_file.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = oauth_token_dir() / candidate.name
        if token := _token_from_file(candidate):
            return token
    return _token_from_file(server_token_path(server_name))


def store_server_token(server_name: str, token: str) -> Path:
    """Persist a bearer token for ``coderai mcp login`` (0600)."""
    from coderai.utils.io import atomic_json_write

    path = server_token_path(server_name)
    atomic_json_write({"access_token": token.strip()}, path)
    with suppress(OSError):
        path.chmod(0o600)
    return path


def clear_server_token(server_name: str) -> bool:
    path = server_token_path(server_name)
    try:
        path.unlink()
        return True
    except OSError:
        return False


def has_server_token(server_name: str, config: dict[str, Any] | None = None) -> bool:
    if config is not None and resolve_server_bearer_token(config, server_name):
        return True
    return _token_from_file(server_token_path(server_name)) is not None


def apply_bearer_auth(
    config: dict[str, Any], server_name: str
) -> dict[str, Any]:
    """Return a copy of an MCP server config with the bearer header applied."""
    if config.get("auth") != "oauth":
        return config
    headers = dict(config.get("headers") or {})
    if any(k.lower() == "authorization" for k in headers):
        return config
    token = resolve_server_bearer_token(config, server_name)
    if not token:
        return config
    return {**config, "headers": {**headers, "Authorization": f"Bearer {token}"}}


def create_mcp_oauth(server_url: str) -> Any:
    """Create fastmcp OAuth adapter for server URL if available."""
    try:
        from fastmcp.client.auth.oauth import OAuth
        return OAuth(mcp_url=server_url)
    except Exception:
        return None


async def has_mcp_oauth_tokens(server_url: str) -> bool:
    """Check whether OAuth tokens exist for this MCP server."""
    token = _token_from_file(server_token_path(server_url))
    return token is not None
